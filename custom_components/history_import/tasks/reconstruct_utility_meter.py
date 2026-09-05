"""Recorder maintenance operations for Utility Meter reconstruction."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
import math
from typing import Any
from zoneinfo import ZoneInfo

from cronsim import CronSim
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticDataTimestamp
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.recorder import session_scope

from ..recorder_handling import refresh_recorder_caches
from ._helpers import (
    floor_period,
    format_timestamp,
    get_or_create_attributes_id,
    get_or_create_states_metadata_id,
    get_or_create_statistics_metadata_id,
)
from .import_history import delete_states
from .recalculate_statistics import delete_all_statistics

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Reconstruct utility meter implementation
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class _ReplayUtilityMeterState:
    state: str
    last_updated_ts: float
    last_reported_ts: float
    last_period: str
    last_valid_state: str | None
    last_reset_ts: float


@dataclass(slots=True)
class UtilityMeterReconstructionPlan:
    """Prepared Utility Meter replay which has not yet modified target history."""

    target_states: list[_ReplayUtilityMeterState]
    source_entity_id: str
    meter_period: str
    source_states_read: int
    oldest_source_timestamp: float | None
    newest_source_timestamp: float | None
    final_native_value: str | None
    final_last_period: str
    final_last_reset_ts: float
    final_last_valid_state: str | None


@dataclass(slots=True)
class UtilityMeterReconstructionResult:
    entity_id: str
    source_entity_id: str
    meter_period: str
    source_states_read: int
    states_deleted: int
    states_rebuilt: int
    short_term_statistics_rebuilt: int
    long_term_statistics_rebuilt: int
    oldest_source_timestamp: float | None
    newest_source_timestamp: float | None
    final_state_timestamp: float
    final_state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity_id,
            "source": self.source_entity_id,
            "meter_period": self.meter_period,
            "source_states_read": self.source_states_read,
            "states_deleted": self.states_deleted,
            "states_rebuilt": self.states_rebuilt,
            "short_term_statistics_rebuilt": self.short_term_statistics_rebuilt,
            "long_term_statistics_rebuilt": self.long_term_statistics_rebuilt,
            "oldest_source_timestamp": format_timestamp(self.oldest_source_timestamp),
            "newest_source_timestamp": format_timestamp(self.newest_source_timestamp),
            "final_state_timestamp": format_timestamp(self.final_state_timestamp),
            "final_state": self.final_state,
        }


def prepare_reconstruct_utility_meter(
    recorderInstance,
    source_entity_id: str,
    meter_period: str,
    cron_pattern: str,
    time_zone: str,
    delta_values: bool,
    net_consumption: bool,
    periodically_resetting: bool,
    sensor_always_available: bool,
    replay_end_timestamp: float,
    chunk_size: int,
) -> UtilityMeterReconstructionPlan:
    """Calculate a Utility Meter replay without modifying target history."""

    with session_scope(session=recorderInstance.get_session()) as session:

        source_states_metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(
                StatesMeta.entity_id == source_entity_id
            )
        )
        if source_states_metadata_id is None:
            raise ValueError(
                f"No Recorder state metadata is available for {source_entity_id}"
            )

        source_states = list(
            session.scalars(
                select(States)
                .where(
                    States.metadata_id == source_states_metadata_id,
                    States.last_updated_ts <= replay_end_timestamp,
                )
                .order_by(States.last_updated_ts.asc(), States.state_id.asc())
            ).all()
        )
        if not source_states:
            raise ValueError(
                f"No Recorder state history is available for {source_entity_id}"
            )

        oldest_source_timestamp = source_states[0].last_updated_ts
        newest_source_timestamp = source_states[-1].last_updated_ts
        if oldest_source_timestamp is None:
            raise ValueError("Recorder source state has no timestamp")

        reset_timestamps = _utility_meter_reset_timestamps(
            cron_pattern,
            time_zone,
            oldest_source_timestamp,
            replay_end_timestamp,
        )
        replay = _replay_utility_meter_states(
            source_states,
            reset_timestamps,
            delta_values,
            net_consumption,
            periodically_resetting,
            sensor_always_available,
        )

    return UtilityMeterReconstructionPlan(
        target_states=replay["target_states"],
        source_entity_id=source_entity_id,
        meter_period=meter_period,
        source_states_read=len(source_states),
        oldest_source_timestamp=oldest_source_timestamp,
        newest_source_timestamp=newest_source_timestamp,
        final_native_value=replay["final_native_value"],
        final_last_period=replay["final_last_period"],
        final_last_reset_ts=replay["final_last_reset_ts"],
        final_last_valid_state=replay["final_last_valid_state"],
    )


def perform_reconstruct_utility_meter(
    recorderInstance,
    target_entity_id: str,
    target_attributes: dict[str, Any],
    current_attributes: dict[str, Any],
    target_statistics_metadata: dict[str, Any],
    state_class: SensorStateClass,
    replay_end_timestamp: float,
    plan: UtilityMeterReconstructionPlan,
    current_state: str,
    current_state_timestamp: float,
    chunk_size: int,
) -> UtilityMeterReconstructionResult:
    """Replace Utility Meter history after the running meter has been corrected."""

    with session_scope(session=recorderInstance.get_session()) as session:

        target_states_metadata_id = get_or_create_states_metadata_id(
            session,
            target_entity_id,
        )
        target_statistics_metadata_id = get_or_create_statistics_metadata_id(
            recorderInstance,
            session,
            target_entity_id,
            target_statistics_metadata,
        )

        delete_all_statistics(
            session,
            target_statistics_metadata_id,
        )
        target_states_deleted = delete_states(
            recorderInstance,
            session,
            target_states_metadata_id,
            cutoff = None,
        )
        session.commit()

        current_attributes_id = get_or_create_attributes_id(
            session,
            current_attributes,
        )

        merge_latest = bool(
            plan.target_states
            and plan.target_states[-1].state == current_state
            and _utility_meter_state_attributes(
                target_attributes,
                plan.target_states[-1],
            ) == current_attributes
        )
        if merge_latest:
            latest_historical_state = plan.target_states[-1]
            anchor_last_updated_ts = latest_historical_state.last_updated_ts
            anchor_last_reported_ts = max(
                current_state_timestamp,
                latest_historical_state.last_reported_ts,
            )
            historical_states = plan.target_states[:-1]
        else:
            anchor_last_updated_ts = current_state_timestamp
            anchor_last_reported_ts = current_state_timestamp
            historical_states = plan.target_states

        current_anchor = States(
            metadata_id      = target_states_metadata_id,
            state            = current_state,
            attributes_id    = current_attributes_id,
            origin_idx       = 0,
            last_updated_ts  = anchor_last_updated_ts,
            last_changed_ts  = None,
            last_reported_ts = anchor_last_reported_ts,
        )
        session.add(current_anchor)
        session.flush()
        if current_anchor.state_id is None:
            raise ValueError("Current Recorder state has no id")
        current_anchor_state_id = current_anchor.state_id
        session.commit()

        try:
            refresh_recorder_caches(
                recorderInstance,
                target_entity_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Utility Meter reconstruction current state committed, but Recorder cache refresh failed"
            )

        _insert_utility_meter_history(
            session,
            target_states_metadata_id,
            target_attributes,
            historical_states,
            current_anchor_state_id,
            chunk_size,
        )

        statistics_result = _rebuild_utility_meter_statistics(
            session,
            target_statistics_metadata_id,
            plan.target_states,
            state_class,
            replay_end_timestamp,
            chunk_size,
        )

    _LOGGER.info(
        "Utility Meter reconstruction: transaction committed; %d states rebuilt, "
        "%d short-term statistics rebuilt, %d hourly statistics rebuilt",
        len(plan.target_states),
        statistics_result["short_term"],
        statistics_result["hourly"],
    )

    return UtilityMeterReconstructionResult(
        entity_id=target_entity_id,
        source_entity_id=plan.source_entity_id,
        meter_period=plan.meter_period,
        source_states_read=plan.source_states_read,
        states_deleted=target_states_deleted,
        states_rebuilt=len(plan.target_states),
        short_term_statistics_rebuilt=statistics_result["short_term"],
        long_term_statistics_rebuilt=statistics_result["hourly"],
        oldest_source_timestamp=plan.oldest_source_timestamp,
        newest_source_timestamp=plan.newest_source_timestamp,
        final_state_timestamp=current_state_timestamp,
        final_state=current_state,
    )


def _utility_meter_reset_timestamps(
    cron_pattern: str,
    time_zone: str,
    start_timestamp: float,
    end_timestamp: float,
) -> list[float]:
    """Return the Utility Meter reset boundary at/before start and through end."""

    local_tz = ZoneInfo(time_zone)
    local_start = datetime.fromtimestamp(start_timestamp, UTC).astimezone(local_tz)
    reverse_start = local_start + timedelta(seconds=1)
    first_reset = next(CronSim(cron_pattern, reverse_start, reverse=True))

    reset_timestamps = [first_reset.timestamp()]
    scheduler = CronSim(cron_pattern, first_reset)
    while True:
        next_reset = next(scheduler)
        next_reset_timestamp = next_reset.timestamp()
        if next_reset_timestamp > end_timestamp:
            break
        reset_timestamps.append(next_reset_timestamp)

    return reset_timestamps


def _replay_utility_meter_states(
    source_states: list[States],
    reset_timestamps: list[float],
    delta_values: bool,
    net_consumption: bool,
    periodically_resetting: bool,
    sensor_always_available: bool,
) -> dict[str, Any]:
    """Replay source Recorder states using Utility Meter sensor semantics."""

    if not reset_timestamps:
        raise ValueError("Utility Meter replay has no reset boundary")

    target_states: list[_ReplayUtilityMeterState] = []
    native_value: Decimal | None = None
    last_period = Decimal(0)
    last_valid_state: Decimal | None = None
    last_reset_ts = reset_timestamps[0]
    available = True
    previous_source_state: str | None = None
    reset_index = 1

    def write_target_state(timestamp: float) -> None:
        state = (
            STATE_UNAVAILABLE
            if not available
            else STATE_UNKNOWN
            if native_value is None
            else str(native_value)
        )
        replay_state = _ReplayUtilityMeterState(
            state=state,
            last_updated_ts=timestamp,
            last_reported_ts=timestamp,
            last_period=str(last_period),
            last_valid_state=(
                str(last_valid_state) if last_valid_state is not None else None
            ),
            last_reset_ts=last_reset_ts,
        )

        if target_states:
            previous = target_states[-1]
            if (
                previous.state == replay_state.state
                and previous.last_period == replay_state.last_period
                and previous.last_valid_state == replay_state.last_valid_state
                and previous.last_reset_ts == replay_state.last_reset_ts
            ):
                previous.last_reported_ts = timestamp
                return

        target_states.append(replay_state)

    def reset_meter(timestamp: float) -> None:
        nonlocal native_value, last_period, last_reset_ts

        last_reset_ts = timestamp
        last_period = native_value if native_value else Decimal(0)
        native_value = Decimal(0)
        write_target_state(timestamp)

    for source_state in source_states:
        timestamp = source_state.last_updated_ts
        if timestamp is None:
            raise ValueError("Recorder source state has no timestamp")

        while (
            reset_index < len(reset_timestamps)
            and reset_timestamps[reset_index] <= timestamp
        ):
            reset_meter(reset_timestamps[reset_index])
            reset_index += 1

        if source_state.state == STATE_UNAVAILABLE:
            if not sensor_always_available:
                available = False
                write_target_state(timestamp)
            previous_source_state = source_state.state
            continue

        available = True
        new_state = _decimal_state(source_state.state)
        if new_state is None:
            previous_source_state = source_state.state
            continue

        if native_value is None:
            native_value = Decimal(0)

        adjustment: Decimal | None
        if delta_values:
            adjustment = new_state
        elif not periodically_resetting and last_valid_state is not None:
            adjustment = new_state - last_valid_state
        else:
            old_state = _decimal_state(previous_source_state)
            adjustment = (
                new_state - old_state
                if old_state is not None
                else None
            )

        if adjustment is not None and (net_consumption or adjustment >= 0):
            native_value += adjustment

        last_valid_state = new_state
        write_target_state(timestamp)
        previous_source_state = source_state.state

    while reset_index < len(reset_timestamps):
        reset_meter(reset_timestamps[reset_index])
        reset_index += 1

    if not target_states:
        raise ValueError("Utility Meter replay produced no target history")

    return {
        "target_states": target_states,
        "final_native_value": (
            str(native_value) if native_value is not None else None
        ),
        "final_last_period": str(last_period),
        "final_last_reset_ts": last_reset_ts,
        "final_last_valid_state": (
            str(last_valid_state) if last_valid_state is not None else None
        ),
    }


def _insert_utility_meter_history(
    session: Session,
    target_states_metadata_id: int,
    target_attributes: dict[str, Any],
    target_states: list[_ReplayUtilityMeterState],
    current_anchor_state_id: int,
    chunk_size: int,
) -> None:
    """Insert prepared Utility Meter states and connect the current anchor."""

    previous_target_row: States | None = None
    previous_target_state_id: int | None = None
    states_inserted = 0
    attributes_ids: dict[tuple[str, str | None, float], int | None] = {}

    for target_state in target_states:
        attributes_key = (
            target_state.last_period,
            target_state.last_valid_state,
            target_state.last_reset_ts,
        )
        if attributes_key not in attributes_ids:
            attributes_ids[attributes_key] = get_or_create_attributes_id(
                session,
                _utility_meter_state_attributes(
                    target_attributes,
                    target_state,
                ),
            )

        db_state = States(
            metadata_id      = target_states_metadata_id,
            state            = target_state.state,
            attributes_id    = attributes_ids[attributes_key],
            origin_idx       = 0,
            last_updated_ts  = target_state.last_updated_ts,
            last_changed_ts  = None,
            last_reported_ts = target_state.last_reported_ts,
        )
        if previous_target_row is not None:
            db_state.old_state = previous_target_row
        elif previous_target_state_id is not None:
            db_state.old_state_id = previous_target_state_id

        session.add(db_state)
        previous_target_row = db_state
        states_inserted += 1

        if states_inserted % chunk_size == 0:
            session.flush()
            if db_state.state_id is None:
                raise ValueError("Rebuilt Recorder state has no id")
            previous_target_state_id = db_state.state_id
            session.commit()
            previous_target_row = None
            _LOGGER.info(
                "Utility Meter reconstruction: committed state chunk; %d states rebuilt",
                states_inserted,
            )

    session.flush()
    if previous_target_row is not None:
        if previous_target_row.state_id is None:
            raise ValueError("Rebuilt Recorder state has no id")
        previous_target_state_id = previous_target_row.state_id

    session.execute(
        update(States)
        .where(States.state_id == current_anchor_state_id)
        .values(old_state_id=previous_target_state_id)
    )
    session.commit()


def _utility_meter_state_attributes(
    target_attributes: dict[str, Any],
    target_state: _ReplayUtilityMeterState,
) -> dict[str, Any]:
    """Build Recorder attributes for one reconstructed Utility Meter state."""

    attributes = dict(target_attributes)
    attributes.pop("next_reset", None)
    attributes["last_period"] = target_state.last_period
    attributes["last_valid_state"] = (
        target_state.last_valid_state
        if target_state.last_valid_state is not None
        else "None"
    )
    attributes["last_reset"] = format_timestamp(target_state.last_reset_ts)
    return attributes


def _rebuild_utility_meter_statistics(
    session: Session,
    statistics_metadata_id: int,
    target_states: list[_ReplayUtilityMeterState],
    state_class: SensorStateClass,
    replay_end_timestamp: float,
    chunk_size: int,
) -> dict[str, int]:
    """Rebuild complete Utility Meter short- and long-term sum statistics."""

    numeric_events: list[tuple[float, float, float, float]] = []
    previous_value: float | None = None
    cycle_start_value: float | None = None
    completed_sum = 0.0
    previous_last_reset_ts: float | None = None

    for target_state in target_states:
        try:
            value = float(target_state.state)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue

        if cycle_start_value is None:
            cycle_start_value = value
            previous_value = value
            previous_last_reset_ts = target_state.last_reset_ts
            current_sum = 0.0
        else:
            if state_class == SensorStateClass.TOTAL_INCREASING:
                if value < 0:
                    continue
                reset = (
                    previous_value is not None
                    and value < 0.9 * previous_value
                )
            else:
                reset = target_state.last_reset_ts != previous_last_reset_ts

            if reset:
                if previous_value is not None:
                    completed_sum += previous_value - cycle_start_value
                cycle_start_value = 0.0

            current_sum = completed_sum + value - cycle_start_value
            previous_value = value
            previous_last_reset_ts = target_state.last_reset_ts

        numeric_events.append(
            (
                target_state.last_updated_ts,
                value,
                current_sum,
                target_state.last_reset_ts,
            )
        )

    if not numeric_events:
        return {"short_term": 0, "hourly": 0}

    created_ts = datetime.now(UTC).timestamp()
    short_start = floor_period(numeric_events[0][0], 5 * 60)
    short_end = floor_period(replay_end_timestamp, 5 * 60)

    short_term_statistics_rebuilt = 0
    pointer = 0
    active_event: tuple[float, float, float, float] | None = None
    bucket_start = short_start

    while bucket_start < short_end:
        bucket_end = bucket_start + 5 * 60

        while pointer < len(numeric_events) and numeric_events[pointer][0] < bucket_end:
            active_event = numeric_events[pointer]
            pointer += 1

        if active_event is not None:
            _, active_value, active_sum, active_last_reset_ts = active_event
            stats: StatisticDataTimestamp = {
                "start_ts": bucket_start,
                "state": active_value,
                "sum": active_sum,
            }
            if state_class != SensorStateClass.TOTAL_INCREASING:
                stats["last_reset_ts"] = active_last_reset_ts
            session.add(
                StatisticsShortTerm.from_stats_ts(
                    statistics_metadata_id,
                    stats,
                    created_ts,
                )
            )
            short_term_statistics_rebuilt += 1
            if short_term_statistics_rebuilt % chunk_size == 0:
                session.commit()

        bucket_start = bucket_end

    session.commit()

    hourly_statistics_rebuilt = 0
    hour_start = floor_period(short_start, 60 * 60)
    hour_end = floor_period(replay_end_timestamp, 60 * 60)

    short_rows = list(
        session.scalars(
            select(StatisticsShortTerm)
            .where(
                StatisticsShortTerm.metadata_id == statistics_metadata_id,
                StatisticsShortTerm.start_ts >= hour_start,
                StatisticsShortTerm.start_ts < hour_end,
            )
            .order_by(StatisticsShortTerm.start_ts.asc())
        ).all()
    )

    by_hour: dict[float, tuple[float | None, float | None, float | None]] = {}
    for row in short_rows:
        if row.start_ts is None:
            continue
        by_hour[floor_period(row.start_ts, 60 * 60)] = (
            row.state,
            row.sum,
            row.last_reset_ts,
        )

    hour = hour_start
    while hour < hour_end:
        if (row_values := by_hour.get(hour)) is not None:
            row_state, row_sum, row_last_reset_ts = row_values
            if row_state is not None and row_sum is not None:
                stats: StatisticDataTimestamp = {
                    "start_ts": hour,
                    "state": row_state,
                    "sum": row_sum,
                }
                if row_last_reset_ts is not None:
                    stats["last_reset_ts"] = row_last_reset_ts
                session.add(
                    Statistics.from_stats_ts(
                        statistics_metadata_id,
                        stats,
                        created_ts,
                    )
                )
                hourly_statistics_rebuilt += 1
                if hourly_statistics_rebuilt % chunk_size == 0:
                    session.commit()
        hour += 60 * 60

    session.commit()

    return {
        "short_term": short_term_statistics_rebuilt,
        "hourly": hourly_statistics_rebuilt,
    }


def _decimal_state(state: str | None) -> Decimal | None:
    """Convert a Recorder state to Decimal using Utility Meter rules."""
    if state is None:
        return None
    try:
        return Decimal(state)
    except (InvalidOperation, TypeError):
        return None

#endregion
#--------------------------------------------------------------------------------
