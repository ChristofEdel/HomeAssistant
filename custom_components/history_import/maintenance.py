"""Recorder maintenance operations for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.helpers.recorder import session_scope

from .importer import (
    ImportMode,
    _delete_states,
    _rebuild_statistics,
    _refresh_recorder_caches,
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
) -> dict[str, Any]:
    """Delete all statistics and rebuild them from the complete state history."""

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
            )
            short_rebuilt = rebuild_result["short_term"]
            long_rebuilt = rebuild_result["hourly"]

    # The database transaction has committed successfully at this point
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
) -> dict[str, Any]:
    """Replace target Recorder history and statistics with a source copy."""

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

        # Delete all existing target records
        _delete_all_statistics(
            session,
            target_statistics_metadata_id,
        )
        target_states_deleted = _delete_states(
            recorderInstance,
            session,
            target_states_metadata_id,
            cutoff = None,
        )

        # Copy all state columns. Database identity / entity reference fields
        # must be regenerated or remapped for the target sensor.
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

        # Copy every mapped statistics column other than the row identity and
        # metadata_id, which necessarily belong to the target sensor.
        for source_row in source_short_statistics:
            values = _copy_column_values(
                StatisticsShortTerm,
                source_row,
                excluded={"id", "metadata_id"},
            )
            values["metadata_id"] = target_statistics_metadata_id
            session.add(StatisticsShortTerm(**values))

        for source_row in source_long_statistics:
            values = _copy_column_values(
                Statistics,
                source_row,
                excluded={"id", "metadata_id"},
            )
            values["metadata_id"] = target_statistics_metadata_id
            session.add(Statistics(**values))

        session.flush()

        oldest_timestamp = (
            source_states[0].last_updated_ts if source_states else None
        )
        newest_timestamp = (
            source_states[-1].last_updated_ts if source_states else None
        )

    # The database transaction has committed successfully at this point
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
) -> None:
    """Delete all short- and long-term statistics for a statistics metadata ID."""
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


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()

#endregion
#--------------------------------------------------------------------------------
