"""Action for selecting recorder history to push to InfluxDB."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
import logging
from typing import Any

import voluptuous as vol
from sqlalchemy import Select, and_, or_, select

from homeassistant.components.recorder.db_schema import (
    SHARED_ATTR_OR_LEGACY_ATTRIBUTES,
    StateAttributes,
    States,
    StatesMeta,
)
from homeassistant.components.recorder.entity_options import is_entity_recorded
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from homeassistant.helpers.recorder import DATA_INSTANCE, get_instance, session_scope
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, json_loads_object

from .const import DOMAIN
from .event_to_json import get_event_to_json
from .influx_connection import InfluxClient, get_influx_connection

_LOGGER = logging.getLogger(__name__)
PUSH_WORKER_KEY = f"{DOMAIN}_push_worker"

PUSH_TO_INFLUXDB_SCHEMA = vol.Schema({
    vol.Optional("entities"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("entity_globs"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("from"): cv.datetime,
    vol.Optional("back_until"): cv.datetime,
    vol.Optional("from_days"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("back_until_days"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Required("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
})


#--------------------------------------------------------------------------------------------------
# region Push worker - process queued history requests
#--------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PushRequest:
    """One entity and range queued for the history push worker."""

    entity_id: str
    from_time: datetime
    back_until: datetime
    chunk_size: int


class PushWorker:
    """Process queued history push requests for a config entry."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self._queue: asyncio.Queue[PushRequest] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._hass = hass
        self._config = config

    def start(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Start the worker for the lifetime of the config entry."""
        self._task = entry.async_create_background_task(
            hass=hass,
            target=self._run(),
            name="simple_influxdb history push worker",
            eager_start=False,
        )

    def enqueue(self, request: PushRequest) -> None:
        """Queue an entity for processing."""
        if self._task is None or self._task.done():
            raise ServiceValidationError("Simple InfluxDB history push worker is not running")
        self._queue.put_nowait(request)

    async def async_shutdown(self) -> None:
        """Cancel the worker when the config entry unloads."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """Process requests in queue order until the entry unloads."""
        while True:
            request = await self._queue.get()
            try:
                result = await push_entity_to_influxdb(
                    request.entity_id, request.from_time, request.back_until, request.chunk_size,
                    self._hass, self._config,
                )
                _LOGGER.info("%s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("InfluxDB history push failed for %s", request.entity_id)
            finally:
                self._queue.task_done()
            await asyncio.sleep(0)


async def async_push_to_influxdb(call: ServiceCall) -> ServiceResponse:
    """Validate the action input and queue entities for a background push."""
    data = call.data
    hass = call.hass
    config: dict[str, Any] | None = hass.data.get(DOMAIN)
    if config is None:
        raise ServiceValidationError("Simple InfluxDB has no loaded config entry")
    worker: PushWorker | None = hass.data.get(PUSH_WORKER_KEY)
    if worker is None:
        raise ServiceValidationError("Simple InfluxDB history push worker is not running")

    if not data.get("entities") and not data.get("entity_globs"):
        raise ServiceValidationError("Specify entities or entity_globs")

    has_absolute = "from" in data or "back_until" in data
    has_days = "from_days" in data or "back_until_days" in data
    if has_absolute == has_days:
        raise ServiceValidationError("Specify either from/back_until or from_days/back_until_days")
    if has_absolute:
        if "from" not in data or "back_until" not in data:
            raise ServiceValidationError("Both from and back_until are required")
        from_time = dt_util.as_utc(data["from"])
        back_until = dt_util.as_utc(data["back_until"])
    else:
        if "from_days" not in data or "back_until_days" not in data:
            raise ServiceValidationError("Both from_days and back_until_days are required")
        midnight = dt_util.start_of_local_day()
        from_time = dt_util.as_utc(midnight + timedelta(days=1 - data["from_days"]))
        back_until = dt_util.as_utc(midnight - timedelta(days=data["back_until_days"]))

    if back_until >= from_time:
        raise ServiceValidationError("back_until must be before from")

    selected = set(data.get("entities", []))
    globs = data.get("entity_globs", [])
    if globs and DATA_INSTANCE in hass.data:
        entity_filter = convert_include_exclude_filter(config)
        known_entities = set(hass.states.async_entity_ids()) | set(er.async_get(hass).entities)
        for entity_id in known_entities:
            if (
                entity_id not in selected
                and any(fnmatchcase(entity_id, glob) for glob in globs)
                and entity_filter(entity_id)
                and is_entity_recorded(hass, entity_id)
            ):
                selected.add(entity_id)

    entities = sorted(selected)
    for entity_id in entities:
        worker.enqueue(PushRequest(entity_id, from_time, back_until, data["chunk_size"]))

    if call.return_response:
        response: JsonObjectType = {
            "output": "History push requests queued",
            "from": from_time.isoformat(),
            "back_until": back_until.isoformat(),
            "chunk_size": data["chunk_size"],
            "entities": entities,
        }
        return response
    return None

# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Push entity to InfluxDB - read recorder chunks and write progress
#--------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HistoryCursor:
    """The last recorder row processed in a paginated read."""

    timestamp: float
    state_id: int


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """A recorder state row for history processing."""

    state_id: int
    timestamp: float
    state: State | None


def _history_select(entity_id: str) -> Select:
    """Select stored states and their attributes for one entity."""
    return (
        select(
            States.state_id, States.last_updated_ts, States.state,
            States.last_changed_ts, SHARED_ATTR_OR_LEGACY_ATTRIBUTES,
        )
        .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
        .outerjoin(StateAttributes, States.attributes_id == StateAttributes.attributes_id)
        .where(StatesMeta.entity_id == entity_id)
    )


def _state_from_row(entity_id: str, row: Any) -> State | None:
    """Recreate a state from a recorder row, including recorded attributes."""
    fields = row._mapping
    timestamp = fields["last_updated_ts"]
    value = fields["state"]
    last_changed = fields["last_changed_ts"]
    attributes = fields["attributes"]
    if value is None or value == "":
        return None  # Recorder stores removed entities as NULL or, in older data, an empty state.
    updated = dt_util.utc_from_timestamp(timestamp)
    return State(
        entity_id=entity_id,
        state=value,
        attributes=json_loads_object(attributes) if attributes else {},
        last_changed=dt_util.utc_from_timestamp(last_changed) if last_changed else updated,
        last_updated=updated,
    )


def _read_previous_state(
    hass: HomeAssistant, entity_id: str, back_until: datetime,
) -> State | None:
    """Read the state before the range for change based point generation."""
    stmt = (
        _history_select(entity_id)
        .where(States.last_updated_ts < back_until.timestamp())
        .order_by(States.last_updated_ts.desc(), States.state_id.desc())
        .limit(1)
    )
    with session_scope(hass=hass, read_only=True) as session:
        row = session.execute(statement=stmt).first()
    return _state_from_row(entity_id, row) if row is not None else None


def _read_history_chunk(
    hass: HomeAssistant, entity_id: str, back_until: datetime, from_time: datetime,
    chunk_size: int, cursor: HistoryCursor | None,
) -> list[HistoryRow]:
    """Read up to chunk_size rows, ordered by timestamp and recorder row ID."""
    stmt = _history_select(entity_id).where(
        States.last_updated_ts >= back_until.timestamp(),
        States.last_updated_ts < from_time.timestamp(),
    )
    if cursor is not None:
        stmt = stmt.where(or_(
            States.last_updated_ts > cursor.timestamp,
            and_(States.last_updated_ts == cursor.timestamp, States.state_id > cursor.state_id),
        ))
    stmt = stmt.order_by(States.last_updated_ts, States.state_id).limit(chunk_size)
    with session_scope(hass=hass, read_only=True) as session:
        rows = session.execute(statement=stmt).all()
    return [
        HistoryRow(
            row._mapping["state_id"], row._mapping["last_updated_ts"],
            _state_from_row(entity_id, row),
        )
        for row in rows
    ]


def _history_chunk_to_points(
    entity_id: str, rows: list[HistoryRow], previous_state: State | None,
    event_to_json: Callable[[Event], list[dict[str, Any]] | None],
) -> tuple[list[dict[str, Any]], State | None]:
    """Format recorder rows exactly as live state change events are formatted."""
    points: list[dict[str, Any]] = []
    for row in rows:
        event = Event(
            event_type=EVENT_STATE_CHANGED,
            data={"entity_id": entity_id, "old_state": previous_state, "new_state": row.state},
            time_fired_timestamp=row.timestamp,
        )
        points.extend(event_to_json(event) or [])
        previous_state = row.state
    return points, previous_state


def _write_history_chunk(
    influx: InfluxClient, entity_id: str, rows: list[HistoryRow],
    points: list[dict[str, Any]],
) -> None:
    """Write a chunk, then record its progress after the write succeeds."""
    if points:
        influx.write(points)

    progress: dict[str, Any] = {
        "measurement": "push_actions",
        "time": dt_util.utcnow(),
        "tags": {"entity_id": entity_id},
        "fields": {
            "state_rows_read": len(rows),
            "objects_written": len(points),
            "time_from": dt_util.utc_from_timestamp(rows[0].timestamp).isoformat(),
            "time_to": dt_util.utc_from_timestamp(rows[-1].timestamp).isoformat(),
        },
    }
    influx.write([progress])


async def push_entity_to_influxdb(
    entity_id: str, from_time: datetime, back_until: datetime, chunk_size: int,
    hass: HomeAssistant, config: dict[str, Any],
) -> str:
    """Copy recorder states to InfluxDB in bounded chunks and record progress."""
    if chunk_size < 1 or back_until >= from_time:
        raise ValueError("Invalid history push range or chunk size")

    recorder = get_instance(hass)
    event_to_json = get_event_to_json(hass, config, False)
    influx = await hass.async_add_executor_job(get_influx_connection, config)
    rows_written = 0
    objects_written = 0
    cursor: HistoryCursor | None = None
    try:
        previous_state = await recorder.async_add_executor_job(
            _read_previous_state, hass, entity_id, back_until,
        )
        while rows := await recorder.async_add_executor_job(
            _read_history_chunk, hass, entity_id, back_until, from_time, chunk_size, cursor,
        ):
            points, previous_state = await hass.async_add_executor_job(
                _history_chunk_to_points, entity_id, rows, previous_state, event_to_json,
            )
            write_job = hass.async_add_executor_job(
                _write_history_chunk, influx, entity_id, rows, points,
            )
            try:
                await asyncio.shield(write_job)
            except asyncio.CancelledError:
                await write_job  # Finish the active write before closing the client on reload.
                raise
            rows_written += len(rows)
            objects_written += len(points)
            cursor = HistoryCursor(rows[-1].timestamp, rows[-1].state_id)
    finally:
        await hass.async_add_executor_job(influx.close)

    return f"Pushed {rows_written} state rows and {objects_written} objects for {entity_id}"

# endregion
#--------------------------------------------------------------------------------------------------
