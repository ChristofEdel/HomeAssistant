"""Recorder maintenance operations for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import logging
import math
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticDataTimestamp
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.recorder import session_scope

from .importer import (
    ImportMode,
    _commit_insert_chunk,
    _commit_stage,
    _delete_states,
    delete_all_statistics,
    _get_or_create_attributes_id,
    _rebuild_statistics,
    _refresh_recorder_caches,
    _validate_chunk_size,
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recalculate implementation
#--------------------------------------------------------------------------------

def perform_recalculate(
    recorderInstance,
    entity_id: str,
    statistics_metadata: dict[str, Any],
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Delete all statistics and rebuild them from the complete state history."""

    _validate_chunk_size(chunk_size)

    with session_scope(session=recorderInstance.get_session()) as session:

        # Get or create statistics metadata
        statistics_metadata_id = _get_or_create_statistics_metadata_id(
            recorderInstance,
            session,
            entity_id,
            statistics_metadata,
        )

        states_metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(StatesMeta.entity_id == entity_id)
        )

        states_read = 0
        oldest_state_timestamp: float | None = None
        newest_state_timestamp: float | None = None

        if states_metadata_id is not None:
            states_read = session.scalar(
                select(func.count())
                .select_from(States)
                .where(States.metadata_id == states_metadata_id)
            ) or 0
            oldest_state_timestamp = session.scalar(
                select(func.min(States.last_updated_ts)).where(
                    States.metadata_id == states_metadata_id
                )
            )
            newest_state_timestamp = session.scalar(
                select(func.max(States.last_updated_ts)).where(
                    States.metadata_id == states_metadata_id
                )
            )

        if states_metadata_id is None or oldest_state_timestamp is None:
            _delete_all_statistics(
                session,
                statistics_metadata_id,
                chunk_size,
            )
            short_rebuilt = 0
            long_rebuilt = 0
        else:
            # Re-use the importer's REPLACE statistics path. This deletes all
            # existing short/long-term statistics and rebuilds the complete range.
            rebuild_result = _rebuild_statistics(
                session,
                ImportMode.REPLACE,
                rebuild_start_timestamp = oldest_state_timestamp,
                rebuild_end_timestamp   = None,
                states_metadata_id      = states_metadata_id,
                statistics_metadata_id  = statistics_metadata_id,
                chunk_size              = chunk_size,
            )
            short_rebuilt = rebuild_result["short_term"]
            long_rebuilt = rebuild_result["hourly"]

    # The database changes have committed successfully at this point
    _LOGGER.info(
        "Statistics recalculation: transaction committed; "
        "%d short-term statistics rebuilt, %d hourly statistics rebuilt",
        short_rebuilt,
        long_rebuilt,
    )
    try:
        _refresh_recorder_caches(
            recorderInstance,
            entity_id,
            statistics_metadata_id,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Statistics recalculation committed, but Recorder cache refresh failed"
        )

    return {
        "entity": entity_id,
        "states_read": states_read,
        "short_term_statistics_rebuilt": short_rebuilt,
        "long_term_statistics_rebuilt": long_rebuilt,
        "oldest_state_timestamp": _format_timestamp(oldest_state_timestamp),
        "newest_state_timestamp": _format_timestamp(newest_state_timestamp),
    }

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Copy implementation
#--------------------------------------------------------------------------------

def perform_copy(
    recorderInstance,
    source_entity_id: str,
    target_entity_id: str,
    target_statistics_metadata: dict[str, Any],
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Replace target Recorder history and statistics with a source copy."""

    _validate_chunk_size(chunk_size)

    with session_scope(session=recorderInstance.get_session()) as session:

        # Resolve the state metadata IDs
        source_states_metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(
                StatesMeta.entity_id == source_entity_id
            )
        )
        target_states_metadata_id = _get_or_create_states_metadata_id(
            session,
            target_entity_id,
        )

        # Resolve the statistics metadata IDs
        source_statistics_metadata = recorderInstance.statistics_meta_manager.get(
            session,
            source_entity_id,
        )
        source_statistics_metadata_id = (
            source_statistics_metadata[0]
            if source_statistics_metadata is not None
            else None
        )
        target_statistics_metadata_id = _get_or_create_statistics_metadata_id(
            recorderInstance,
            session,
            target_entity_id,
            target_statistics_metadata,
        )

        # Read all source records before modifying the target
        source_states: list[States] = []
        if source_states_metadata_id is not None:
            source_states = list(
                session.scalars(
                    select(States)
                    .where(States.metadata_id == source_states_metadata_id)
                    .order_by(States.last_updated_ts.asc(), States.state_id.asc())
                ).all()
            )

        source_short_statistics: list[StatisticsShortTerm] = []
        source_long_statistics: list[Statistics] = []
        if source_statistics_metadata_id is not None:
            source_short_statistics = list(
                session.scalars(
                    select(StatisticsShortTerm)
                    .where(
                        StatisticsShortTerm.metadata_id
                        == source_statistics_metadata_id
                    )
                    .order_by(
                        StatisticsShortTerm.start_ts.asc(),
                        StatisticsShortTerm.id.asc(),
                    )
                ).all()
            )
            source_long_statistics = list(
                session.scalars(
                    select(Statistics)
                    .where(Statistics.metadata_id == source_statistics_metadata_id)
                    .order_by(Statistics.start_ts.asc(), Statistics.id.asc())
                ).all()
            )

        oldest_timestamp = (
            source_states[0].last_updated_ts if source_states else None
        )
        newest_timestamp = (
            source_states[-1].last_updated_ts if source_states else None
        )

        source_state_records: list[tuple[int | None, int | None, dict[str, Any]]] = []
        source_short_values: list[dict[str, Any]] = []
        source_long_values: list[dict[str, Any]] = []
        if chunk_size is not None:
            for source_row in source_states:
                values = _copy_column_values(
                    States,
                    source_row,
                    excluded={"state_id", "old_state_id", "metadata_id", "entity_id"},
                )
                source_state_records.append(
                    (source_row.state_id, source_row.old_state_id, values)
                )
            source_short_values = [
                _copy_column_values(
                    StatisticsShortTerm,
                    source_row,
                    excluded={"id", "metadata_id"},
                )
                for source_row in source_short_statistics
            ]
            source_long_values = [
                _copy_column_values(
                    Statistics,
                    source_row,
                    excluded={"id", "metadata_id"},
                )
                for source_row in source_long_statistics
            ]

        # Delete all existing target records
        _delete_all_statistics(
            session,
            target_statistics_metadata_id,
            chunk_size,
        )
        target_states_deleted = _delete_states(
            recorderInstance,
            session,
            target_states_metadata_id,
            cutoff = None
        )

        # Copy all state columns. Database identity / entity reference fields
        # must be regenerated or remapped for the target sensor.
        if chunk_size is None:
            copied_state_rows: list[tuple[States, States]] = []
            for source_row in source_states:
                values = _copy_column_values(
                    States,
                    source_row,
                    excluded={"state_id", "old_state_id", "metadata_id", "entity_id"},
                )
                values["metadata_id"] = target_states_metadata_id

                if "entity_id" in States.__table__.columns:
                    values["entity_id"] = target_entity_id

                target_row = States(**values)
                session.add(target_row)
                copied_state_rows.append((source_row, target_row))

            # Generate state IDs before rebuilding the old_state_id chain
            session.flush()

            state_id_map = {
                source_row.state_id: target_row.state_id
                for source_row, target_row in copied_state_rows
                if source_row.state_id is not None and target_row.state_id is not None
            }
            for source_row, target_row in copied_state_rows:
                if source_row.old_state_id is not None:
                    target_row.old_state_id = state_id_map.get(source_row.old_state_id)
        else:
            state_id_map: dict[int, int] = {}
            current_chunk_rows: dict[int, States] = {}
            states_copied = 0

            for source_state_id, source_old_state_id, source_values in source_state_records:
                values = dict(source_values)
                values["metadata_id"] = target_states_metadata_id

                if "entity_id" in States.__table__.columns:
                    values["entity_id"] = target_entity_id

                target_row = States(**values)
                if source_old_state_id is not None:
                    if source_old_state_id in current_chunk_rows:
                        target_row.old_state = current_chunk_rows[source_old_state_id]
                    elif source_old_state_id in state_id_map:
                        target_row.old_state_id = state_id_map[source_old_state_id]

                session.add(target_row)
                states_copied += 1
                if source_state_id is not None:
                    current_chunk_rows[source_state_id] = target_row

                if states_copied % chunk_size == 0:
                    session.flush()
                    for pending_source_id, pending_target_row in current_chunk_rows.items():
                        if pending_target_row.state_id is None:
                            raise ValueError("Copied Recorder state has no id")
                        state_id_map[pending_source_id] = pending_target_row.state_id
                    session.commit()
                    current_chunk_rows.clear()
                    _LOGGER.info(
                        "History copy: committed state chunk; %d states copied",
                        states_copied,
                    )

            session.flush()
            for pending_source_id, pending_target_row in current_chunk_rows.items():
                if pending_target_row.state_id is None:
                    raise ValueError("Copied Recorder state has no id")
                state_id_map[pending_source_id] = pending_target_row.state_id
            _commit_stage(session, chunk_size)

        # Copy every mapped statistics column other than the row identity and
        # metadata_id, which necessarily belong to the target sensor.
        if chunk_size is None:
            for source_row in source_short_statistics:
                values = _copy_column_values(
                    StatisticsShortTerm,
                    source_row,
                    excluded={"id", "metadata_id"},
                )
                values["metadata_id"] = target_statistics_metadata_id
                session.add(StatisticsShortTerm(**values))
        else:
            for count, source_values in enumerate(source_short_values, start=1):
                values = dict(source_values)
                values["metadata_id"] = target_statistics_metadata_id
                session.add(StatisticsShortTerm(**values))
                _commit_insert_chunk(session, chunk_size, count)
        _commit_stage(session, chunk_size)

        if chunk_size is None:
            for source_row in source_long_statistics:
                values = _copy_column_values(
                    Statistics,
                    source_row,
                    excluded={"id", "metadata_id"},
                )
                values["metadata_id"] = target_statistics_metadata_id
                session.add(Statistics(**values))
        else:
            for count, source_values in enumerate(source_long_values, start=1):
                values = dict(source_values)
                values["metadata_id"] = target_statistics_metadata_id
                session.add(Statistics(**values))
                _commit_insert_chunk(session, chunk_size, count)

        session.flush()
        _commit_stage(session, chunk_size)

    # The database changes have committed successfully at this point
    _LOGGER.info(
        "History copy: transaction committed; %d states copied, "
        "%d short-term statistics copied, %d long-term statistics copied",
        states_copied,
        len(source_short_statistics),
        len(source_long_statistics),
    )
    try:
        _refresh_recorder_caches(
            recorderInstance,
            target_entity_id,
            target_statistics_metadata_id,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "History copy committed, but Recorder cache refresh failed"
        )

    return {
        "sensor_from": source_entity_id,
        "sensor_to": target_entity_id,
        "states_deleted": target_states_deleted,
        "states_copied": len(source_states),
        "short_term_statistics_copied": len(source_short_statistics),
        "long_term_statistics_copied": len(source_long_statistics),
        "oldest_copied_timestamp": _format_timestamp(oldest_timestamp),
        "newest_copied_timestamp": _format_timestamp(newest_timestamp),
    }

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Reintegrate implementation
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class ReintegrationResult:
    """Result returned by the Recorder thread after a historical reintegration."""

    response: dict[str, Any]
    final_internal_state: str | None
    final_state_timestamp: float
    final_trigger: str


@dataclass(slots=True)
class _ReplaySourceState:
    state: str | None
    last_updated_ts: float | None
    last_reported_ts: float | None


@dataclass(slots=True)
class _ReplayTargetState:
    state: str
    last_updated_ts: float
    last_reported_ts: float


@dataclass(slots=True)
class ReintegrationPlan:
    """Prepared Integral replay which has not yet modified the target history."""

    target_states: list[_ReplayTargetState]
    source_entity_id: str
    method: str
    round_digits: int | None
    max_sub_interval: float | None
    source_states_read: int
    oldest_source_timestamp: float | None
    newest_source_timestamp: float | None
    final_internal_state: str | None
    final_exposed_state: str
    final_state_timestamp: float
    final_trigger: str


def prepare_reintegrate(
    recorderInstance,
    source_entity_id: str,
    method: str,
    round_digits: int | None,
    unit_prefix: int,
    unit_time: int,
    max_sub_interval: float | None,
    replay_end_timestamp: float,
    chunk_size: int | None = None,
) -> ReintegrationPlan:
    """Calculate an Integral replay without modifying the target Recorder history."""

    _validate_chunk_size(chunk_size)

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

        source_states: list[States | _ReplaySourceState]
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
        replay_source_states: list[States | _ReplaySourceState]
        if chunk_size is None:
            replay_source_states = source_states
        else:
            replay_source_states = [
                _ReplaySourceState(
                    state=row.state,
                    last_updated_ts=row.last_updated_ts,
                    last_reported_ts=row.last_reported_ts,
                )
                for row in source_states
            ]

        replay = _replay_integration_states(
            replay_source_states,
            method,
            round_digits,
            unit_prefix,
            unit_time,
            max_sub_interval,
            replay_end_timestamp,
        )

    return ReintegrationPlan(
        target_states=replay["target_states"],
        source_entity_id=source_entity_id,
        method=method,
        round_digits=round_digits,
        max_sub_interval=max_sub_interval,
        source_states_read=len(source_states),
        oldest_source_timestamp=oldest_source_timestamp,
        newest_source_timestamp=newest_source_timestamp,
        final_internal_state=replay["final_internal_state"],
        final_exposed_state=replay["final_exposed_state"],
        final_state_timestamp=replay["final_state_timestamp"],
        final_trigger=replay["final_trigger"],
    )


def perform_reintegrate(
    recorderInstance,
    target_entity_id: str,
    target_attributes: dict[str, Any],
    target_statistics_metadata: dict[str, Any],
    round_digits: int | None,
    replay_end_timestamp: float,
    plan: ReintegrationPlan,
    current_internal_state: str | None,
    current_available: bool,
    current_state_timestamp: float,
    chunk_size: int | None = None,
) -> ReintegrationResult:
    """Replace Integral history after the running sensor has been corrected."""

    _validate_chunk_size(chunk_size)

    with session_scope(session=recorderInstance.get_session()) as session:

        target_states_metadata_id = _get_or_create_states_metadata_id(
            session,
            target_entity_id,
        )
        target_statistics_metadata_id = _get_or_create_statistics_metadata_id(
            recorderInstance,
            session,
            target_entity_id,
            target_statistics_metadata,
        )

        _delete_all_statistics(
            session,
            target_statistics_metadata_id
        )
        target_states_deleted = _delete_states(
            recorderInstance,
            session,
            target_states_metadata_id,
            cutoff = None,
        )

        attributes_id = _get_or_create_attributes_id(session, target_attributes)

        internal_state = (
            Decimal(current_internal_state)
            if current_internal_state is not None
            else None
        )
        current_exposed_state = _integration_exposed_state(
            internal_state,
            current_available,
            round_digits,
        )

        merge_latest = bool(
            plan.target_states
            and plan.target_states[-1].state == current_exposed_state
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
            state            = current_exposed_state,
            attributes_id    = attributes_id,
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
            _refresh_recorder_caches(
                recorderInstance,
                target_entity_id,
                target_statistics_metadata_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Historical reintegration current state committed, but Recorder cache refresh failed"
            )

        _insert_reintegration_history(
            session,
            target_states_metadata_id,
            attributes_id,
            historical_states,
            current_anchor_state_id,
            chunk_size,
        )

        statistics_result = _rebuild_total_statistics(
            session,
            target_states_metadata_id,
            target_statistics_metadata_id,
            replay_end_timestamp,
            chunk_size,
        )

    _LOGGER.info(
        "Historical reintegration: transaction committed; %d states rebuilt, "
        "%d short-term statistics rebuilt, %d hourly statistics rebuilt",
        len(plan.target_states),
        statistics_result["short_term"],
        statistics_result["hourly"],
    )
    try:
        _refresh_recorder_caches(
            recorderInstance,
            target_entity_id,
            target_statistics_metadata_id,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Historical reintegration committed, but Recorder cache refresh failed"
        )

    response = {
        "entity": target_entity_id,
        "source": plan.source_entity_id,
        "method": plan.method,
        "round_digits": plan.round_digits,
        "max_sub_interval_seconds": plan.max_sub_interval,
        "source_states_read": plan.source_states_read,
        "states_deleted": target_states_deleted,
        "states_rebuilt": len(plan.target_states),
        "short_term_statistics_rebuilt": statistics_result["short_term"],
        "long_term_statistics_rebuilt": statistics_result["hourly"],
        "oldest_source_timestamp": _format_timestamp(plan.oldest_source_timestamp),
        "newest_source_timestamp": _format_timestamp(plan.newest_source_timestamp),
        "final_state_timestamp": _format_timestamp(current_state_timestamp),
        "final_state": current_exposed_state,
    }

    return ReintegrationResult(
        response=response,
        final_internal_state=current_internal_state,
        final_state_timestamp=current_state_timestamp,
        final_trigger=plan.final_trigger,
    )


def _insert_reintegration_history(
    session: Session,
    target_states_metadata_id: int,
    attributes_id: int | None,
    target_states: list[_ReplayTargetState],
    current_anchor_state_id: int,
    chunk_size: int | None = None,
) -> None:
    """Insert prepared historical states and connect the current anchor to them."""

    previous_target_row: States | None = None
    previous_target_state_id: int | None = None
    states_inserted = 0

    for target_state in target_states:
        db_state = States(
            metadata_id      = target_states_metadata_id,
            state            = target_state.state,
            attributes_id    = attributes_id,
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

        if chunk_size is not None and states_inserted % chunk_size == 0:
            session.flush()
            if db_state.state_id is None:
                raise ValueError("Rebuilt Recorder state has no id")
            previous_target_state_id = db_state.state_id
            session.commit()
            previous_target_row = None
            _LOGGER.info(
                "Historical reintegration: committed state chunk; %d states rebuilt",
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
    _commit_stage(session, chunk_size)


def _replay_integration_states(
    source_states: list[States | _ReplaySourceState],
    method: str,
    round_digits: int | None,
    unit_prefix: int,
    unit_time: int,
    max_sub_interval: float | None,
    replay_end_timestamp: float,
) -> dict[str, Any]:
    """Replay source Recorder states using Home Assistant Integral semantics."""

    first_source = source_states[0]
    if first_source.last_updated_ts is None:
        raise ValueError("Recorder source state has no timestamp")

    internal_state: Decimal | None = None
    previous_source_state = first_source.state
    last_integration_timestamp = first_source.last_updated_ts
    target_states: list[_ReplayTargetState] = []
    previous_exposed_state: str | None = None
    final_trigger = "state_event"

    def write_target_state(timestamp: float, available: bool) -> None:
        nonlocal previous_exposed_state

        exposed_state = _integration_exposed_state(
            internal_state,
            available,
            round_digits,
        )

        if previous_exposed_state == exposed_state:
            target_states[-1].last_reported_ts = timestamp
            return

        target_states.append(
            _ReplayTargetState(
                state=exposed_state,
                last_updated_ts=timestamp,
                last_reported_ts=timestamp,
            )
        )
        previous_exposed_state = exposed_state

    def integrate_constant_until(timestamp: float) -> None:
        """Reproduce timer integrations up to, but not including, an event."""
        nonlocal internal_state, last_integration_timestamp, final_trigger

        if max_sub_interval is None or max_sub_interval <= 0:
            return

        constant_state = _decimal_state(previous_source_state)
        if constant_state is None:
            return

        while last_integration_timestamp + max_sub_interval < timestamp:
            timer_timestamp = last_integration_timestamp + max_sub_interval
            internal_state = _add_integral_area(
                internal_state,
                constant_state * Decimal(str(max_sub_interval)),
                unit_prefix,
                unit_time,
            )
            write_target_state(timer_timestamp, True)
            last_integration_timestamp = timer_timestamp
            final_trigger = "time_elapsed"

    def process_same_state_report(timestamp: float) -> None:
        """Integrate one retained same-state report and restart the timer clock."""
        nonlocal internal_state, last_integration_timestamp, final_trigger

        if timestamp <= last_integration_timestamp:
            return

        integrate_constant_until(timestamp)
        constant_state = _decimal_state(previous_source_state)
        if constant_state is not None:
            elapsed = Decimal(str(timestamp - last_integration_timestamp))
            internal_state = _add_integral_area(
                internal_state,
                constant_state * elapsed,
                unit_prefix,
                unit_time,
            )
            write_target_state(timestamp, True)
        elif previous_source_state == STATE_UNAVAILABLE:
            write_target_state(timestamp, False)
        else:
            write_target_state(timestamp, True)

        last_integration_timestamp = timestamp
        final_trigger = "state_event"

    # The live Integral sensor has no value when it first sees its source. It only
    # establishes the initial source state and starts accumulating on a later event.
    write_target_state(
        first_source.last_updated_ts,
        first_source.state != STATE_UNAVAILABLE,
    )

    for index, source_state in enumerate(source_states[1:], start=1):
        timestamp = source_state.last_updated_ts
        if timestamp is None:
            raise ValueError("Recorder source state has no timestamp")

        # Recorder collapses repeated reports of an unchanged source into the same
        # states row and retains only its final last_reported_ts. Processing that final
        # report preserves the accumulated value because all omitted reports integrate
        # the same constant source value. Their individual historical timestamps cannot
        # be reconstructed once Recorder has collapsed them.
        previous_source_row = source_states[index - 1]
        report_timestamp = previous_source_row.last_reported_ts
        if (
            report_timestamp is not None
            and report_timestamp > last_integration_timestamp
            and report_timestamp < timestamp
        ):
            process_same_state_report(report_timestamp)

        if timestamp < last_integration_timestamp:
            continue

        integrate_constant_until(timestamp)

        # An unavailable source makes the Integral sensor unavailable and does not
        # integrate the preceding interval. The source event still restarts the
        # max_sub_interval clock.
        if source_state.state == STATE_UNAVAILABLE:
            write_target_state(timestamp, False)
            previous_source_state = source_state.state
            last_integration_timestamp = timestamp
            final_trigger = "state_event"
            continue

        elapsed = Decimal(str(timestamp - last_integration_timestamp))
        area = _calculate_integration_area(
            method,
            elapsed,
            previous_source_state,
            source_state.state,
        )
        if area is not None:
            internal_state = _add_integral_area(
                internal_state,
                area,
                unit_prefix,
                unit_time,
            )

        write_target_state(timestamp, True)
        previous_source_state = source_state.state
        last_integration_timestamp = timestamp
        final_trigger = "state_event"

    # Process the final retained same-state report, if there was one after the last
    # state change and before the replay cut-off.
    final_report_timestamp = source_states[-1].last_reported_ts
    if (
        final_report_timestamp is not None
        and final_report_timestamp > last_integration_timestamp
        and final_report_timestamp <= replay_end_timestamp
    ):
        process_same_state_report(final_report_timestamp)

    # Continue full max_sub_interval updates beyond the last recorded source event.
    # Do not manufacture a partial interval at the service cut-off: the running
    # Integral sensor is synchronised to the final completed replay event and resumes
    # the remaining interval from there.
    if max_sub_interval is not None and max_sub_interval > 0:
        constant_state = _decimal_state(previous_source_state)
        if constant_state is not None:
            while last_integration_timestamp + max_sub_interval <= replay_end_timestamp:
                timer_timestamp = last_integration_timestamp + max_sub_interval
                internal_state = _add_integral_area(
                    internal_state,
                    constant_state * Decimal(str(max_sub_interval)),
                    unit_prefix,
                    unit_time,
                )
                write_target_state(timer_timestamp, True)
                last_integration_timestamp = timer_timestamp
                final_trigger = "time_elapsed"

    return {
        "target_states": target_states,
        "states_rebuilt": len(target_states),
        "final_internal_state": (
            str(internal_state) if internal_state is not None else None
        ),
        "final_exposed_state": _integration_exposed_state(
            internal_state,
            previous_source_state != STATE_UNAVAILABLE,
            round_digits,
        ),
        "final_state_timestamp": last_integration_timestamp,
        "final_source_state": previous_source_state,
        "final_trigger": final_trigger,
    }


def _calculate_integration_area(
    method: str,
    elapsed: Decimal,
    left_state: str | None,
    right_state: str | None,
) -> Decimal | None:
    """Calculate one unscaled Riemann-sum area using Integral sensor semantics."""

    left = _decimal_state(left_state)
    right = _decimal_state(right_state)

    if method == "left":
        if left is None:
            return None
        return left * elapsed

    if method == "right":
        if right is None:
            return None
        return right * elapsed

    if method == "trapezoidal":
        if left is None or right is None:
            return None
        return elapsed * (left + right) / 2

    raise ValueError(f"Unsupported integration method: {method}")


def _add_integral_area(
    current_state: Decimal | None,
    area: Decimal,
    unit_prefix: int,
    unit_time: int,
) -> Decimal:
    """Scale one area to the target Integral unit and add it to the accumulator."""
    area_scaled = area / Decimal(unit_prefix * unit_time)
    return area_scaled if current_state is None else current_state + area_scaled


def _integration_exposed_state(
    internal_state: Decimal | None,
    available: bool,
    round_digits: int | None,
) -> str:
    """Return the state string Home Assistant would expose for the accumulator."""
    if not available:
        return STATE_UNAVAILABLE
    if internal_state is None:
        return STATE_UNKNOWN
    if round_digits:
        return str(round(internal_state, round_digits))
    return str(internal_state)


def _decimal_state(state: str | None) -> Decimal | None:
    """Convert a Recorder state to Decimal using Integral sensor rules."""
    if state is None:
        return None
    try:
        return Decimal(state)
    except (InvalidOperation, TypeError):
        return None


def _rebuild_total_statistics(
    session: Session,
    states_metadata_id: int,
    statistics_metadata_id: int,
    replay_end_timestamp: float,
    chunk_size: int | None = None,
) -> dict[str, int]:
    """Rebuild complete total-sensor short- and long-term statistics."""

    rows = list(
        session.scalars(
            select(States)
            .where(States.metadata_id == states_metadata_id)
            .order_by(States.last_updated_ts.asc(), States.state_id.asc())
        ).all()
    )

    numeric_values: list[tuple[float, float]] = []
    for row in rows:
        if row.state is None or row.last_updated_ts is None:
            continue
        try:
            value = float(row.state)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        numeric_values.append((row.last_updated_ts, value))

    if not numeric_values:
        return {"short_term": 0, "hourly": 0}

    created_ts = datetime.now(UTC).timestamp()
    baseline = numeric_values[0][1]
    short_start = _floor_period(numeric_values[0][0], 5 * 60)
    short_end = _floor_period(replay_end_timestamp, 5 * 60)

    short_term_statistics_rebuilt = 0
    pointer = 0
    active_value: float | None = None
    bucket_start = short_start

    while bucket_start < short_end:
        bucket_end = bucket_start + 5 * 60

        while pointer < len(numeric_values) and numeric_values[pointer][0] < bucket_end:
            active_value = numeric_values[pointer][1]
            pointer += 1

        if active_value is not None:
            stats: StatisticDataTimestamp = {
                "start_ts": bucket_start,
                "state": active_value,
                "sum": active_value - baseline,
            }
            session.add(
                StatisticsShortTerm.from_stats_ts(
                    statistics_metadata_id,
                    stats,
                    created_ts,
                )
            )
            short_term_statistics_rebuilt += 1
            _commit_insert_chunk(
                session,
                chunk_size,
                short_term_statistics_rebuilt,
            )

        bucket_start = bucket_end

    session.flush()
    _commit_stage(session, chunk_size)

    hourly_statistics_rebuilt = 0
    hour_start = _floor_period(short_start, 60 * 60)
    hour_end = _floor_period(replay_end_timestamp, 60 * 60)

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
        by_hour[_floor_period(row.start_ts, 60 * 60)] = (
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
                _commit_insert_chunk(
                    session,
                    chunk_size,
                    hourly_statistics_rebuilt,
                )
        hour += 60 * 60

    _commit_stage(session, chunk_size)

    return {
        "short_term": short_term_statistics_rebuilt,
        "hourly": hourly_statistics_rebuilt,
    }


#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Supporting functions
#--------------------------------------------------------------------------------

def _get_or_create_states_metadata_id(
    session: Session,
    entity_id: str,
) -> int:
    """Get or create the Recorder states metadata row for an entity."""
    states_metadata = session.scalar(
        select(StatesMeta).where(StatesMeta.entity_id == entity_id)
    )
    if states_metadata is None:
        states_metadata = StatesMeta(entity_id=entity_id)
        session.add(states_metadata)
        session.flush()

    assert states_metadata.metadata_id is not None
    return states_metadata.metadata_id


def _get_or_create_statistics_metadata_id(
    recorderInstance,
    session: Session,
    entity_id: str,
    statistics_metadata: dict[str, Any],
) -> int:
    """Get or create the Recorder statistics metadata row for an entity."""
    query_result = recorderInstance.statistics_meta_manager.get(
        session,
        entity_id,
    )
    if query_result:
        statistics_metadata_id, _ = query_result
        return statistics_metadata_id

    _, statistics_metadata_id = (
        recorderInstance.statistics_meta_manager.update_or_add(
            session,
            statistics_metadata,
            {},
        )
    )
    session.flush()

    assert statistics_metadata_id is not None
    return statistics_metadata_id


def _delete_all_statistics(
    session: Session,
    statistics_metadata_id: int,
    chunk_size: int | None = None,
) -> None:
    """Delete all short- and long-term statistics for a statistics metadata ID."""
    if chunk_size is None:
        session.execute(
            delete(StatisticsShortTerm).where(
                StatisticsShortTerm.metadata_id == statistics_metadata_id
            )
        )
        session.execute(
            delete(Statistics).where(
                Statistics.metadata_id == statistics_metadata_id
            )
        )
        return

    delete_all_statistics(
        session,
        statistics_metadata_id,
    )
    _commit_stage(session, chunk_size)


def _copy_column_values(
    model,
    source_row,
    *,
    excluded: set[str],
) -> dict[str, Any]:
    """Copy all mapped table columns except explicitly replaced columns."""
    return {
        column.key: getattr(source_row, column.key)
        for column in model.__table__.columns
        if column.key not in excluded and not column.primary_key
    }


def _floor_period(timestamp: float, period: int) -> float:
    return float(math.floor(timestamp / period) * period)


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()

#endregion
#--------------------------------------------------------------------------------
