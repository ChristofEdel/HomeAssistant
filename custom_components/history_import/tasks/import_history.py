"""Recorder import and statistics rebuilding for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from homeassistant.components.recorder import purge as recorder_purge
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.recorder import session_scope
import yaml

from ..csv_reader import Sample
from .recalculate_statistics import rebuild_statistics_with_commit
from ._helpers import (
    ImportMode,
    get_or_create_attributes_id,
    get_or_create_states_metadata_id,
    get_or_create_statistics_metadata_id,
    format_timestamp,
)
_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region shared data structuress
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class ImportResult:
    entity_id: str
    mode: str
    states_in_file: int
    states_imported: int = 0
    states_deleted: int = 0
    states_skipped: int = 0
    short_term_statistics_rebuilt: int = 0
    hourly_statistics_rebuilt: int = 0
    oldest_imported_timestamp: float | None = None
    newest_imported_timestamp: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity_id,
            "mode": self.mode,
            "states_in_file": self.states_in_file,
            "states_imported": self.states_imported,
            "states_deleted": self.states_deleted,
            "states_skipped": self.states_skipped,
            "short_term_statistics_rebuilt": self.short_term_statistics_rebuilt,
            "hourly_statistics_rebuilt": self.hourly_statistics_rebuilt,
            "oldest_imported_timestamp": format_timestamp(self.oldest_imported_timestamp),
            "newest_imported_timestamp": format_timestamp(self.newest_imported_timestamp),
        }


#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Main import function + helpers 
#--------------------------------------------------------------------------------

def perform_import(
    recorder_instance,
    entity_id: str,
    entity_attributes: dict[str, Any],
    state_class: SensorStateClass,
    samples: tuple[Sample, ...],
    mode: ImportMode,
    chunk_size: int,
) -> ImportResult:
    """Mutate states and statistics in Recorder."""

    _LOGGER.info("Importing history for %s", entity_id)

    # prepare the result object and a mutable copy of the samples to import
    result = ImportResult(
        entity_id=entity_id,
        mode=mode.value,
        states_in_file=len(samples),
    )
    samples_to_import: tuple[Sample, ...] = samples

    with session_scope(session=recorder_instance.get_session()) as session:
        states_metadata_id = get_or_create_states_metadata_id(session, entity_id)
        statistics_metadata_id = get_or_create_statistics_metadata_id(recorder_instance, session, entity_id)

        # Delete states that will be overwritten, depending on mode
        oldest_existing_state: States | None = None
        oldest_existing_state_id: int | None = None
        oldest_existing_timestamp: float | None = None

        match mode:
            case ImportMode.APPEND:
                # do NOT delete states, but prevent overwriting data
                # by removing newer samples from the import
                oldest_existing_state = session.scalar(
                    select(States)
                    .where(States.metadata_id == states_metadata_id)
                    .order_by(States.last_updated_ts.asc(), States.state_id.asc())
                    .limit(1)
                )
                if oldest_existing_state is not None:
                    oldest_timestamp = oldest_existing_state.last_updated_ts
                    if oldest_timestamp is None:
                        raise ValueError("Recorder state has no timestamp")
                    oldest_existing_state_id = oldest_existing_state.state_id
                    oldest_existing_timestamp = oldest_timestamp
                    samples_to_import = tuple(
                        sample
                        for sample in samples
                        if sample.timestamp < oldest_timestamp
                    )
                    result.states_skipped = len(samples) - len(samples_to_import)
                    if not samples_to_import:
                        return result

            case ImportMode.OVERWRITE:
                # delete all states that are OLDER than the last (newest) sample we want to import
                result.states_deleted = delete_states(
                    recorder_instance,
                    session,
                    states_metadata_id,
                    cutoff=samples[-1].timestamp,
                )
                session.commit()
                # get the oldest existing state that remains in the database
                oldest_existing_state = session.scalar(
                    select(States)
                    .where(
                        States.metadata_id == states_metadata_id,
                        States.last_updated_ts > samples[-1].timestamp,
                    )
                    .order_by(States.last_updated_ts.asc(), States.state_id.asc())
                    .limit(1)
                )
                if oldest_existing_state is not None:
                    oldest_existing_state_id = oldest_existing_state.state_id
                    oldest_existing_timestamp = oldest_existing_state.last_updated_ts

            case ImportMode.REPLACE:
                # Replace mode - remove ALL existing states
                result.states_deleted = delete_states(
                    recorder_instance,
                    session,
                    states_metadata_id,
                    cutoff=None,
                )
                session.commit()

            case _:
                raise ValueError(f"Unsupported import mode: {mode}")

        attributes_id = get_or_create_attributes_id(session, entity_attributes)

        # Now we iterate over all samples and insrt them in the database
        previous: States | None = None
        previous_state_id: int | None = None
        previous_value: float | None = None
        newest_imported_timestamp: float | None = None

        for sample in samples_to_import:
            # Skip unchanged values
            current_value = float(sample.state)
            if previous_value is not None and current_value == previous_value:
                result.states_skipped += 1
                continue

            db_state = States(
                metadata_id      = states_metadata_id,
                state            = sample.state,
                attributes_id    = attributes_id,
                origin_idx       = 0,
                last_updated_ts  = sample.timestamp,
                last_changed_ts  = None,
                last_reported_ts = None,
            )

            if previous is not None:
                db_state.old_state = previous
            elif previous_state_id is not None:
                db_state.old_state_id = previous_state_id

            session.add(db_state)
            result.states_imported += 1
            previous = db_state
            previous_value = current_value
            newest_imported_timestamp = sample.timestamp

            if result.states_imported % chunk_size == 0:
                session.flush()
                if db_state.state_id is None:
                    raise ValueError("Imported Recorder state has no id")
                previous_state_id = db_state.state_id
                session.commit()
                previous = None
                _LOGGER.debug(
                    "History import: committed state chunk; %d states imported",
                    result.states_imported,
                )
            
        # Commit all states and Make inserted states and generated state_ids visible 
        # inside this transaction
        session.commit()
        _LOGGER.debug(
            "History import: committed state history for %s; %d states imported",
            entity_id,
            result.states_imported,
        )

        # Linkk up the last imported state to the oldest existing state, if any
        if previous is not None:
            if previous.state_id is None:
                raise ValueError("Imported Recorder state has no id")
            previous_state_id = previous.state_id

        if previous_state_id is None or newest_imported_timestamp is None:
            raise ValueError("No states were imported")

        if oldest_existing_state_id is not None:
            session.execute(
                update(States)
                .where(States.state_id == oldest_existing_state_id)
                .values(old_state_id=previous_state_id)
            )
        session.commit()

        result.oldest_imported_timestamp = samples_to_import[0].timestamp
        result.newest_imported_timestamp = newest_imported_timestamp

        rebuild_end_timestamp: float | None = None
        if oldest_existing_timestamp is not None:
            rebuild_end_timestamp = oldest_existing_timestamp

        rebuild_result = rebuild_statistics_with_commit(
            session,
            mode,
            state_class=state_class,
            rebuild_start_timestamp=result.oldest_imported_timestamp,
            rebuild_end_timestamp=rebuild_end_timestamp,
            states_metadata_id=states_metadata_id,
            statistics_metadata_id=statistics_metadata_id,
            chunk_size=chunk_size,
        )

        result.short_term_statistics_rebuilt = rebuild_result["short_term"]
        result.hourly_statistics_rebuilt = rebuild_result["hourly"]

    # The database transaction has committed successfully at this point
    _LOGGER.info(
        "Import result:\n%s",
        yaml.safe_dump(result.as_dict(), sort_keys=False),
    )
    return result


def delete_states(
    instance,
    session: Session,
    metadata_id: int,
    cutoff: float | None,
) -> int:
    """Delete states and any normalized attributes which become unused."""
    deleted = 0
    batch_size = max(1, min(1000, instance.max_bind_vars - 10))
    candidate_attributes_ids: set[int] = set()

    while True:
        stmt = select(States.state_id, States.attributes_id).where(
            States.metadata_id == metadata_id
        )
        if cutoff is not None:
            stmt = stmt.where(States.last_updated_ts <= cutoff)
        rows = session.execute(stmt.limit(batch_size)).all()
        if not rows:
            break

        state_ids = {state_id for state_id, _ in rows}
        candidate_attributes_ids.update(
            attributes_id
            for _, attributes_id in rows
            if attributes_id is not None
        )
        recorder_purge._purge_state_ids(  # noqa: SLF001
            instance,
            session,
            state_ids,
        )
        session.flush()
        deleted += len(state_ids)

    recorder_purge._purge_unused_attributes_ids(  # noqa: SLF001
        instance,
        session,
        candidate_attributes_ids,
    )
    return deleted

#endregion
#--------------------------------------------------------------------------------
