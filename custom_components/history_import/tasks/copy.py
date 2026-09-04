"""Recorder maintenance operations for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

from homeassistant.components.recorder.db_schema import (
    States,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.helpers.recorder import session_scope
from .recalculate_statistics import delete_all_statistics
from .import_history import delete_states
from ._helpers import (
    get_states_metadata_id,
    get_or_create_states_metadata_id,
    get_statistics_metadata_id,
    get_or_create_statistics_metadata_id
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Copy implementation
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class CopyResult:
    sensor_from: str
    sensor_to: str
    states_copied: int = 0
    short_term_statistics_copied: int = 0
    long_term_statistics_copied: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_from": self.sensor_from,
            "sensor_to": self.sensor_to ,
            "states_copied": self.states_copied,
            "short_term_statistics_copied": self.short_term_statistics_copied,
            "long_term_statistics_copied": self.long_term_statistics_copied,
        }

def perform_copy(
    recorderInstance,
    source_entity_id: str,
    target_entity_id: str,
    target_statistics_metadata: dict[str, Any],
    chunk_size: int,
) -> CopyResult:
    """Replace target Recorder history and statistics with a source copy."""

    _LOGGER.info("Copying %s --> %s", source_entity_id, target_entity_id)

    result = CopyResult(
        sensor_from=source_entity_id,
        sensor_to=target_entity_id,
    )

    with session_scope(session=recorderInstance.get_session()) as session:

        # Resolve the state and statistics metadata IDs
        source_states_metadata_id = get_states_metadata_id(session, source_entity_id)
        target_states_metadata_id = get_or_create_states_metadata_id(session, target_entity_id)
        source_statistics_metadata_id = get_statistics_metadata_id(recorderInstance, session, source_entity_id)
        target_statistics_metadata_id = get_or_create_statistics_metadata_id(
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

        source_state_records: list[tuple[int | None, int | None, dict[str, Any]]] = []
        source_short_values: list[dict[str, Any]] = []
        source_long_values: list[dict[str, Any]] = []

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
        delete_all_statistics(
            session,
            target_statistics_metadata_id
        )
        delete_states(
            recorderInstance,
            session,
            target_states_metadata_id,
            cutoff = None
        )
        session.commit()
        _LOGGER.debug("Copy: Deleted old states and statistics for %s", target_entity_id)

        # Copy all state columns. Database identity / entity reference fields
        # must be regenerated or remapped for the target sensor.
        state_id_map: dict[int, int] = {}
        current_chunk_rows: dict[int, States] = {}
        result.states_copied = 0

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
            result.states_copied += 1
            if source_state_id is not None:
                current_chunk_rows[source_state_id] = target_row

            if result.states_copied % chunk_size == 0:
                session.flush()
                for pending_source_id, pending_target_row in current_chunk_rows.items():
                    if pending_target_row.state_id is None:
                        raise ValueError("Copied Recorder state has no id")
                    state_id_map[pending_source_id] = pending_target_row.state_id
                session.commit()
                current_chunk_rows.clear()
                _LOGGER.debug("Copy: committed state chunk; %d states copied", result.states_copied)

        session.flush()
        for pending_source_id, pending_target_row in current_chunk_rows.items():
            if pending_target_row.state_id is None:
                raise ValueError("Copied Recorder state has no id")
            state_id_map[pending_source_id] = pending_target_row.state_id
        session.commit()
        _LOGGER.debug("Copy: Committed state history for %s, %d states copied", target_entity_id, result.states_copied)

        # Copy all short-term statistics in chunks, remapping the metadata_id to the target sensor
        count = 0
        for count, source_values in enumerate(source_short_values, start=1):
            values = dict(source_values)
            values["metadata_id"] = target_statistics_metadata_id
            session.add(StatisticsShortTerm(**values))
            if count % chunk_size == 0:
                session.commit()
                _LOGGER.debug("Copy: committed statistics chunk; %d statistics copied", count)
        session.commit()
        _LOGGER.debug("Copy: Committed short-term statistics for %s, %d statistics copied", target_entity_id, count)
        result.short_term_statistics_copied = count

        # Copy all long-term statistics in chunks, remapping the metadata_id to the target sensor
        count = 0
        for count, source_values in enumerate(source_long_values, start=1):
            values = dict(source_values)
            values["metadata_id"] = target_statistics_metadata_id
            session.add(Statistics(**values))
            if count % chunk_size == 0:
                session.commit()
                _LOGGER.debug("Copy: committed statistics chunk; %d statistics copied", count)
        session.commit()
        _LOGGER.debug("Copy: Committed long-term statistics for %s, %d statistics copied", target_entity_id, count)
        result.long_term_statistics_copied = count

    # The database changes have committed successfully at this point
    _LOGGER.info(
        "Copy result:\n%s",
        yaml.safe_dump(result.as_dict(), sort_keys=False),
    )
    return result

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Supporting functions
#--------------------------------------------------------------------------------

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

#endregion
#--------------------------------------------------------------------------------
