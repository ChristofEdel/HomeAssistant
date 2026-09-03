"""Recorder maintenance task scaffolding and request handling for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
import logging
from typing import Any

from homeassistant.components.integration.const import (
    METHOD_LEFT,
    METHOD_RIGHT,
    METHOD_TRAPEZOIDAL,
)
from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .importer import recover_recorder_caches_after_failure
from .maintenance import (
    ReintegrationResult,
    perform_copy,
    perform_recalculate,
    perform_reintegrate,
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

_ALLOWED_COPY_STATE_CLASSES = {
    SensorStateClass.MEASUREMENT,
    SensorStateClass.TOTAL,
    SensorStateClass.TOTAL_INCREASING,
}

_INTEGRATION_METHOD_BY_CLASS_NAME = {
    "_Left": METHOD_LEFT,
    "_Right": METHOD_RIGHT,
    "_Trapezoidal": METHOD_TRAPEZOIDAL,
}


class HistoryMaintenanceError(HomeAssistantError):
    """Base class for history maintenance errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder tasks
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class RecalculateRecorderTask(RecorderTask):
    """Recalculate all measurement statistics on the Recorder thread."""

    entity_id: str
    statistics_metadata: dict[str, Any]
    chunk_size: int | None
    future: asyncio.Future[dict[str, Any]]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_recalculate(
                instance,
                self.entity_id,
                self.statistics_metadata,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "Statistics recalculation failed for %s",
                self.entity_id,
            )
            try:
                recover_recorder_caches_after_failure(instance, self.entity_id)
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "Statistics recalculation failed and Recorder cache recovery also failed"
                )
            instance.hass.loop.call_soon_threadsafe(
                _set_future_exception,
                self.future,
                err,
            )
        else:
            instance.hass.loop.call_soon_threadsafe(
                _set_future_result,
                self.future,
                result,
            )


@dataclass(slots=True)
class CopyRecorderTask(RecorderTask):
    """Copy complete Recorder history and statistics on the Recorder thread."""

    source_entity_id: str
    target_entity_id: str
    target_statistics_metadata: dict[str, Any]
    chunk_size: int | None
    future: asyncio.Future[dict[str, Any]]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_copy(
                instance,
                self.source_entity_id,
                self.target_entity_id,
                self.target_statistics_metadata,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "History copy failed from %s to %s",
                self.source_entity_id,
                self.target_entity_id,
            )
            try:
                recover_recorder_caches_after_failure(instance, self.target_entity_id)
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "History copy failed and Recorder cache recovery also failed"
                )
            instance.hass.loop.call_soon_threadsafe(
                _set_future_exception,
                self.future,
                err,
            )
        else:
            instance.hass.loop.call_soon_threadsafe(
                _set_future_result,
                self.future,
                result,
            )


@dataclass(slots=True)
class ReintegrateRecorderTask(RecorderTask):
    """Historically replay an Integral sensor on the Recorder thread."""

    target_entity_id: str
    source_entity_id: str
    target_attributes: dict[str, Any]
    target_statistics_metadata: dict[str, Any]
    method: str
    round_digits: int | None
    unit_prefix: int
    unit_time: int
    max_sub_interval: float | None
    replay_end_timestamp: float
    chunk_size: int | None
    future: asyncio.Future[ReintegrationResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_reintegrate(
                instance,
                self.target_entity_id,
                self.source_entity_id,
                self.target_attributes,
                self.target_statistics_metadata,
                self.method,
                self.round_digits,
                self.unit_prefix,
                self.unit_time,
                self.max_sub_interval,
                self.replay_end_timestamp,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "Historical reintegration failed for %s",
                self.target_entity_id,
            )
            try:
                recover_recorder_caches_after_failure(
                    instance,
                    self.target_entity_id,
                )
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "Historical reintegration failed and Recorder cache recovery also failed"
                )
            instance.hass.loop.call_soon_threadsafe(
                _set_future_exception,
                self.future,
                err,
            )
        else:
            instance.hass.loop.call_soon_threadsafe(
                _set_future_result,
                self.future,
                result,
            )


def _set_future_exception(future: asyncio.Future, err: Exception) -> None:
    if not future.done():
        future.set_exception(err)


def _set_future_result(future: asyncio.Future, result: Any) -> None:
    if not future.done():
        future.set_result(result)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_recalculate_statistics, called by the UI (via handle_recalculate())
#--------------------------------------------------------------------------------

async def async_recalculate_statistics(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Validate a recalculation request, queue it on Recorder, and return the result."""

    # Validate the entity
    entity_state = hass.states.get(entity_id)
    if entity_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if entity_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")
    if entity_state.attributes.get("state_class") != SensorStateClass.MEASUREMENT:
        raise ServiceValidationError(
            f"Entity must have state_class measurement: {entity_id}"
        )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[entity_id],
        )
    )

    recorder_statistics_metadata = statistic_ids.get(entity_id)
    if recorder_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {entity_id}"
        )

    # Recalculate statistics in the database
    instance = get_instance(hass)
    future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    instance.queue_task(
        RecalculateRecorderTask(
            entity_id            = entity_id,
            statistics_metadata = dict(recorder_statistics_metadata),
            chunk_size           = chunk_size,
            future               = future,
        )
    )

    try:
        return await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Statistics recalculation failed: {err}"
        ) from err

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_copy_sensor_records, called by the UI (via handle_copy())
#--------------------------------------------------------------------------------

async def async_copy_sensor_records(
    hass: HomeAssistant,
    *,
    source_entity_id: str,
    target_entity_id: str,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Validate a copy request, queue it on Recorder, and return the result."""

    if source_entity_id == target_entity_id:
        raise ServiceValidationError("Source and target sensors must be different")

    # Validate both entities
    source_state = hass.states.get(source_entity_id)
    if source_state is None:
        raise ServiceValidationError(f"Entity does not exist: {source_entity_id}")

    target_state = hass.states.get(target_entity_id)
    if target_state is None:
        raise ServiceValidationError(f"Entity does not exist: {target_entity_id}")

    for entity_id, entity_state in (
        (source_entity_id, source_state),
        (target_entity_id, target_state),
    ):
        if entity_state.domain != "sensor":
            raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")
        if entity_state.attributes.get("state_class") not in _ALLOWED_COPY_STATE_CLASSES:
            raise ServiceValidationError(
                "Entity must have state_class measurement, total, or "
                f"total_increasing: {entity_id}"
            )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[target_entity_id],
        )
    )

    target_statistics_metadata = statistic_ids.get(target_entity_id)
    if target_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {target_entity_id}"
        )

    # Copy state history and statistics in the database
    instance = get_instance(hass)
    future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    instance.queue_task(
        CopyRecorderTask(
            source_entity_id           = source_entity_id,
            target_entity_id           = target_entity_id,
            target_statistics_metadata = dict(target_statistics_metadata),
            chunk_size                 = chunk_size,
            future                     = future,
        )
    )

    try:
        return await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(f"History copy failed: {err}") from err

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region async_reintegrate_sensor, called by the UI (via handle_reintegrate())
#--------------------------------------------------------------------------------

async def async_reintegrate_sensor(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Validate an Integral replay request, queue it on Recorder, and return the result."""

    # Validate the target entity and resolve the live Integral sensor object. The
    # live object is the authoritative source for the effective Integral settings,
    # whether the sensor was configured through YAML or a config entry.
    target_state = hass.states.get(entity_id)
    if target_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if target_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")

    sensor_component = hass.data.get(DATA_INSTANCES, {}).get("sensor")
    integration_entity = (
        sensor_component.get_entity(entity_id)
        if sensor_component is not None
        else None
    )
    if not isinstance(integration_entity, IntegrationSensor):
        raise ServiceValidationError(
            f"Entity must be an Integral integration sensor: {entity_id}"
        )

    source_entity_id = getattr(integration_entity, "_source_entity")
    if source_entity_id == entity_id:
        raise ServiceValidationError(
            f"Integral source and target sensors must be different: {entity_id}"
        )

    source_state = hass.states.get(source_entity_id)
    if source_state is None:
        raise ServiceValidationError(
            f"Integral source entity does not exist: {source_entity_id}"
        )

    method_object = getattr(integration_entity, "_method")
    method = _INTEGRATION_METHOD_BY_CLASS_NAME.get(type(method_object).__name__)
    if method is None:
        raise ServiceValidationError(
            f"Unsupported Integral method for {entity_id}: "
            f"{type(method_object).__name__}"
        )

    round_digits = getattr(integration_entity, "_round_digits")
    unit_prefix = int(getattr(integration_entity, "_unit_prefix"))
    unit_time = int(getattr(integration_entity, "_unit_time"))
    max_sub_interval_value = getattr(integration_entity, "_max_sub_interval")
    max_sub_interval = (
        max_sub_interval_value.total_seconds()
        if max_sub_interval_value is not None
        else None
    )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[entity_id],
        )
    )

    target_statistics_metadata = statistic_ids.get(entity_id)
    if target_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {entity_id}"
        )

    # Rebuild the target states and statistics on the Recorder thread. The cut-off
    # is fixed before queueing so the replay has one deterministic end point.
    replay_end_timestamp = datetime.now(UTC).timestamp()
    instance = get_instance(hass)
    future: asyncio.Future[ReintegrationResult] = hass.loop.create_future()
    instance.queue_task(
        ReintegrateRecorderTask(
            target_entity_id           = entity_id,
            source_entity_id           = source_entity_id,
            target_attributes          = dict(target_state.attributes),
            target_statistics_metadata = dict(target_statistics_metadata),
            method                     = method,
            round_digits               = round_digits,
            unit_prefix                = unit_prefix,
            unit_time                  = unit_time,
            max_sub_interval           = max_sub_interval,
            replay_end_timestamp       = replay_end_timestamp,
            chunk_size                 = chunk_size,
            future                     = future,
        )
    )

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Historical reintegration failed: {err}"
        ) from err

    # The Recorder database changes are committed. Replace the running Integral sensor's
    # old accumulator with the rebuilt value before its next normal integration.
    _synchronise_live_integration_sensor(
        hass,
        integration_entity,
        source_entity_id,
        result,
        max_sub_interval,
    )

    return result.response


def _synchronise_live_integration_sensor(
    hass: HomeAssistant,
    integration_entity: IntegrationSensor,
    source_entity_id: str,
    result: ReintegrationResult,
    max_sub_interval: float | None,
) -> None:
    """Synchronise the running Integral sensor with the rebuilt Recorder history."""

    if result.final_internal_state is None:
        internal_state = None
    else:
        try:
            internal_state = Decimal(result.final_internal_state)
        except InvalidOperation as err:
            raise HistoryMaintenanceError(
                f"Invalid rebuilt Integral state: {result.final_internal_state}"
            ) from err

    # Cancel any pre-rebuild timer because it was created against the old
    # accumulator and integration clock.
    cancel_timer = getattr(
        integration_entity,
        "_cancel_max_sub_interval_exceeded_callback",
    )
    cancel_timer()

    setattr(integration_entity, "_state", internal_state)
    setattr(integration_entity, "_last_valid_state", internal_state)
    setattr(
        integration_entity,
        "_last_integration_time",
        datetime.fromtimestamp(result.final_state_timestamp, UTC),
    )

    trigger_type = type(getattr(integration_entity, "_last_integration_trigger"))
    trigger = (
        trigger_type.TimeElapsed
        if result.final_trigger == "time_elapsed"
        else trigger_type.StateEvent
    )
    setattr(integration_entity, "_last_integration_trigger", trigger)

    current_source_state = hass.states.get(source_entity_id)
    available = (
        current_source_state is None
        or current_source_state.state != STATE_UNAVAILABLE
    )
    setattr(integration_entity, "_attr_available", available)

    if current_source_state is not None and available:
        derive_attributes = getattr(
            integration_entity,
            "_derive_and_set_attributes_from_state",
        )
        derive_attributes(current_source_state)

    integration_entity.async_write_ha_state()

    # Resume max_sub_interval from the historical integration clock. The first
    # callback may need less than one full interval; after it fires, the Integral
    # sensor's normal scheduler takes over again.
    if (
        max_sub_interval is not None
        and max_sub_interval > 0
        and current_source_state is not None
        and _decimal_state(current_source_state.state) is not None
    ):
        elapsed = max(
            0.0,
            dt_util.utcnow().timestamp() - result.final_state_timestamp,
        )
        delay = max(0.0, max_sub_interval - elapsed)
        _schedule_first_reintegration_interval(
            hass,
            integration_entity,
            source_entity_id,
            delay,
        )


def _schedule_first_reintegration_interval(
    hass: HomeAssistant,
    integration_entity: IntegrationSensor,
    source_entity_id: str,
    delay: float,
) -> None:
    """Schedule the first post-replay max_sub_interval callback."""

    @callback
    def _integrate_after_replay(now: datetime) -> None:
        source_state = hass.states.get(source_entity_id)
        if source_state is None:
            return
        source_value = _decimal_state(source_state.state)
        if source_value is None:
            return

        last_integration_time = getattr(
            integration_entity,
            "_last_integration_time",
        )
        elapsed_seconds = Decimal(
            str((now - last_integration_time).total_seconds())
        )

        derive_attributes = getattr(
            integration_entity,
            "_derive_and_set_attributes_from_state",
        )
        derive_attributes(source_state)

        method = getattr(integration_entity, "_method")
        area = method.calculate_area_with_one_state(
            elapsed_seconds,
            source_value,
        )
        update_integral = getattr(integration_entity, "_update_integral")
        update_integral(area)
        integration_entity.async_write_ha_state()

        setattr(integration_entity, "_last_integration_time", dt_util.utcnow())
        trigger_type = type(getattr(integration_entity, "_last_integration_trigger"))
        setattr(
            integration_entity,
            "_last_integration_trigger",
            trigger_type.TimeElapsed,
        )

        schedule_normal = getattr(
            integration_entity,
            "_schedule_max_sub_interval_exceeded_if_state_is_numeric",
        )
        schedule_normal(source_state)

    cancel_callback = async_call_later(
        hass,
        delay,
        _integrate_after_replay,
    )
    setattr(
        integration_entity,
        "_max_sub_interval_exceeded_callback",
        cancel_callback,
    )


def _decimal_state(state: str) -> Decimal | None:
    """Convert a live source state to Decimal using Integral sensor rules."""
    try:
        return Decimal(state)
    except (InvalidOperation, TypeError):
        return None

#endregion
#--------------------------------------------------------------------------------
