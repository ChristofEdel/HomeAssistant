"""Recorder import and statistics rebuilding for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from datetime import UTC, datetime
import logging
import math
from dataclasses import dataclass
from typing import Any, Final
from enum import StrEnum

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.components.recorder import purge as recorder_purge
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticDataTimestamp
from homeassistant.helpers.json import JSON_DUMP
from homeassistant.helpers.recorder import session_scope

from .csv_reader import Sample

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region shared data structuress
#--------------------------------------------------------------------------------

class ImportMode(StrEnum):
    APPEND = "append"
    OVERWRITE = "overwrite"
    REPLACE = "replace"

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
            "oldest_imported_timestamp": _format_timestamp(self.oldest_imported_timestamp),
            "newest_imported_timestamp": _format_timestamp(self.newest_imported_timestamp),
        }

SHORT_TERM_SECONDS: Final = 5 * 60
LONG_TERM_SECONDS: Final = 60 * 60
MAX_UNCHUNKED_IMPORT_STATES: Final = 10_000

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Main import function + helpers 
#--------------------------------------------------------------------------------

def perform_import(
    recorder_instance,
    entity_id: str,
    entity_attributes: dict[str, Any],
    samples: tuple[Sample, ...],
    mode: ImportMode,
    chunk_size: int | None = None,
) -> ImportResult:
    """Mutate states and statistics in Recorder."""

    _validate_chunk_size(chunk_size)
    if chunk_size is None and len(samples) > MAX_UNCHUNKED_IMPORT_STATES:
        raise ValueError(
            f"Import contains {len(samples)} states; chunk_size is required for "
            f"imports over {MAX_UNCHUNKED_IMPORT_STATES:,} states"
        )

    samples_to_import: tuple[Sample, ...] = samples
    result = ImportResult(
        entity_id=entity_id,
        mode=mode,
        states_in_file=len(samples),
    )

    with session_scope(session=recorder_instance.get_session()) as session:
        states_metadata_id = get_or_create_states_metadata_id(session, entity_id)
        statistics_metadata_id, _ = get_or_create_statistics_metadata(
            recorder_instance,
            session,
            entity_id,
        )


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
                result.states_deleted = _delete_states(
                    recorder_instance,
                    session,
                    states_metadata_id,
                    cutoff=samples[-1].timestamp,
                )
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
                result.states_deleted = _delete_states(
                    recorder_instance,
                    session,
                    states_metadata_id,
                    cutoff=None,
                )

            case _:
                raise ValueError(f"Unsupported import mode: {mode}")

        attributes_id = _get_or_create_attributes_id(session, entity_attributes)

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

            if (
                chunk_size is not None
                and result.states_imported % chunk_size == 0
            ):
                session.flush()
                if db_state.state_id is None:
                    raise ValueError("Imported Recorder state has no id")
                previous_state_id = db_state.state_id
                session.commit()
                previous = None
                _LOGGER.info(
                    "History import: committed state chunk; %d states imported",
                    result.states_imported,
                )

        # Connect the oldest retained existing state (if any) to the last imported state
        if chunk_size is None:
            if oldest_existing_state is not None and previous is not None:
                oldest_existing_state.old_state = previous

        # Make inserted states and generated state_ids visible inside this transaction
        _LOGGER.info("History import:session.flush()")
        session.flush()

        if previous is not None:
            if previous.state_id is None:
                raise ValueError("Imported Recorder state has no id")
            previous_state_id = previous.state_id

        if previous_state_id is None or newest_imported_timestamp is None:
            raise ValueError("No states were imported")

        if (
            chunk_size is not None
            and oldest_existing_state_id is not None
        ):
            _LOGGER.info("History import:fixing up state chain")
            session.    execute(
                update(States)
                .where(States.state_id == oldest_existing_state_id)
                .values(old_state_id=previous_state_id)
            )

        result.oldest_imported_timestamp = samples_to_import[0].timestamp
        result.newest_imported_timestamp = newest_imported_timestamp

        _commit_stage(session, chunk_size)

        rebuild_end_timestamp: float | None = None
        if oldest_existing_timestamp is not None:
            rebuild_end_timestamp = oldest_existing_timestamp

        rebuild_result = _rebuild_statistics(
            session,
            mode,
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
        "History import: transaction committed; %d states imported, "
        "%d short-term statistics rebuilt, %d hourly statistics rebuilt",
        result.states_imported,
        result.short_term_statistics_rebuilt,
        result.hourly_statistics_rebuilt,
    )
    try:
        _refresh_recorder_caches(
            recorder_instance,
            entity_id,
            statistics_metadata_id,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "History import committed, but Recorder cache refresh failed"
        )

    return result


def get_or_create_states_metadata_id(session: Session, entity_id: str) -> int:
    """Return the states metadata id for an entity, creating it if needed."""
    states_metadata = session.scalar(
        select(StatesMeta).where(StatesMeta.entity_id == entity_id)
    )
    if states_metadata is None:
        states_metadata = StatesMeta(entity_id=entity_id)
        session.add(states_metadata)
        session.flush()

    if states_metadata.metadata_id is None:
        raise ValueError(f"Recorder states metadata has no id for {entity_id}")
    return states_metadata.metadata_id


def get_or_create_statistics_metadata(
    recorder_instance,
    session: Session,
    entity_id: str,
    statistics_metadata: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return statistics metadata for an entity, creating it if needed."""
    query_result = recorder_instance.statistics_meta_manager.get(session, entity_id)
    if query_result:
        statistics_metadata_id, existing_metadata = query_result
        return statistics_metadata_id, existing_metadata

    if statistics_metadata is None:
        statistics_metadata = sensor_recorder.list_statistic_ids(
            recorder_instance.hass,
            statistic_ids=[entity_id],
        ).get(entity_id)

    if statistics_metadata is None:
        raise ValueError(f"No Recorder statistics metadata is available for {entity_id}")

    _, statistics_metadata_id = recorder_instance.statistics_meta_manager.update_or_add(
        session,
        statistics_metadata,
        {},
    )
    session.flush()

    if statistics_metadata_id is None:
        raise ValueError(f"Recorder statistics metadata has no id for {entity_id}")
    return statistics_metadata_id, statistics_metadata


def _delete_states(
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

#endregion
#--------------------------------------------------------------------------------
    recorder_purge._purge_unused_attributes_ids(  # noqa: SLF001
        instance,
        session,
        candidate_attributes_ids,
    )
    return deleted


#--------------------------------------------------------------------------------
#region Statistics rebuild function + helpers
#--------------------------------------------------------------------------------
def _rebuild_statistics(
    session: Session,
    mode: ImportMode,
    rebuild_start_timestamp: float,
    rebuild_end_timestamp: float | None,
    states_metadata_id: int,
    statistics_metadata_id: int,
    chunk_size: int | None = None,
) -> dict[str, int]:
    """Delete invalid statistics and rebuild them from Recorder states."""

    now_timestamp = datetime.now(UTC).timestamp()

    # Determine the first statistics buckets affected by the import
    rebuild_short_start = _floor_period(rebuild_start_timestamp, SHORT_TERM_SECONDS)
    rebuild_hour_start = _floor_period(rebuild_start_timestamp, LONG_TERM_SECONDS)

    # Statistics can only be generated for completed periods
    rebuild_short_end_exclusive = _floor_period(now_timestamp, SHORT_TERM_SECONDS)
    rebuild_hour_end_exclusive = _floor_period(now_timestamp, LONG_TERM_SECONDS)
    if rebuild_end_timestamp is not None:
        rebuild_short_end_exclusive = min(
            _ceil_period(rebuild_end_timestamp, SHORT_TERM_SECONDS),
            rebuild_short_end_exclusive,
        )
        rebuild_hour_end_exclusive = min(
            _ceil_period(rebuild_end_timestamp, LONG_TERM_SECONDS),
            rebuild_hour_end_exclusive,
        )

    # Remove statistics made invalid by the state changes
    match mode:
        case ImportMode.APPEND:
                # Only the imported range and the transition into the retained
                # existing history are affected.
            _delete_statistics_range(
                session,
                StatisticsShortTerm,
                statistics_metadata_id,
                rebuild_short_start,
                rebuild_short_end_exclusive,
                chunk_size,
            )
            _delete_statistics_range(
                session,
                Statistics,
                statistics_metadata_id,
                rebuild_hour_start,
                rebuild_hour_end_exclusive,
                chunk_size,
            )

        case ImportMode.OVERWRITE:
                # All historical states before/through the imported range were
                # deleted. Therefore all statistics before the end of the affected
                # range are invalid.
            session.execute(
                delete(StatisticsShortTerm).where(
                    StatisticsShortTerm.metadata_id == statistics_metadata_id,
                    StatisticsShortTerm.start_ts < rebuild_short_end_exclusive,
                )
            )
            session.execute(
                delete(Statistics).where(
                    Statistics.metadata_id == statistics_metadata_id,
                    Statistics.start_ts < rebuild_hour_end_exclusive,
                )
            )

        case ImportMode.REPLACE:
                # All states were replaced, therefore all existing statistics
                # are invalid.
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

    _commit_stage(session, chunk_size)

    # Rebuild 5-minute statistics
    short_term_statistics_rebuilt = 0
    if rebuild_short_end_exclusive > rebuild_short_start:
        short_term_statistics_rebuilt = _rebuild_short_term_statistics(
            session,
            states_metadata_id,
            statistics_metadata_id,
            rebuild_short_start,
            rebuild_short_end_exclusive,
            chunk_size,
        )
        session.flush()

    # Rebuild hourly statistics from the new 5-minute statistics
    hourly_statistics_rebuilt = 0
    if rebuild_hour_end_exclusive > rebuild_hour_start:
        hourly_statistics_rebuilt = _rebuild_hourly_statistics(
            session,
            statistics_metadata_id,
            rebuild_hour_start,
            rebuild_hour_end_exclusive,
            chunk_size,
        )

    return {
        "short_term": short_term_statistics_rebuilt,
        "hourly": hourly_statistics_rebuilt,
    }


def delete_all_statistics(
    session: Session,
    statistics_metadata_id: int
) -> None:
    """Delete all short- and long-term statistics for one metadata id."""
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


def _delete_statistics_range(
    session: Session,
    table,
    metadata_id: int,
    start: float,
    end: float | None,
    chunk_size: int | None = None,
) -> None:
    stmt = delete(table).where(
        table.metadata_id == metadata_id,
        table.start_ts >= start,
    )
    if end is not None:
        stmt = stmt.where(table.start_ts < end)
    session.execute(stmt)
    return


def _rebuild_short_term_statistics(
    session: Session,
    states_metadata_id: int,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int | None = None,
) -> int:
    """Rebuild complete 5-minute measurement buckets from Recorder states."""

    # Get all states from the database
    raw_states = _load_states_for_statistics(
        session,
        states_metadata_id,
        start,
        end,
    )

    # Go over all states we got from the database
    # and place the ones that are valid in 'values'
    # in chronological order
    values: list[tuple[float, float]] = []

    for db_state, _ in raw_states:

        # Put the value into 'value'; ignore non-numeric data
        if db_state.state is None:
            continue
        try:
            value = float(db_state.state)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue

        db_state_last_updated_ts = db_state.last_updated_ts
        if db_state_last_updated_ts is None:
            raise ValueError("Recorder state has no timestamp")
        values.append((db_state_last_updated_ts, value))

    created_ts = datetime.now(UTC).timestamp()
    count = 0
    pointer = 0
    active_value: float | None = None

    # Get the last value at or before the first bucket boundary.
    while pointer < len(values) and values[pointer][0] <= start:
        active_value = values[pointer][1]
        pointer += 1

    # Now we go forward bucket-by-bucket
    bucket_start = start
    while bucket_start < end:
        bucket_end = bucket_start + SHORT_TERM_SECONDS

        min_value: float | None = active_value
        max_value: float | None = active_value
        weighted_total = 0.0
        weighted_seconds = 0.0

        cursor = bucket_start

        while pointer < len(values) and values[pointer][0] < bucket_end:
            timestamp, value = values[pointer]

            if active_value is not None:
                duration = max(0.0, timestamp - cursor)
                weighted_total += active_value * duration
                weighted_seconds += duration

            active_value = value
            cursor = timestamp

            if min_value is None or value < min_value:
                min_value = value
            if max_value is None or value > max_value:
                max_value = value

            pointer += 1

        if active_value is not None:
            duration = max(0.0, bucket_end - cursor)
            weighted_total += active_value * duration
            weighted_seconds += duration

        if min_value is not None and max_value is not None and weighted_seconds > 0:
            mean = weighted_total / weighted_seconds
            stats: StatisticDataTimestamp = {
                "start_ts": bucket_start,
                "mean": mean,
                "min": min_value,
                "max": max_value,
            }
            session.add(
                StatisticsShortTerm.from_stats_ts(
                    statistic_metadata_id,
                    stats,
                    created_ts,
                )
            )
            count += 1
            _commit_insert_chunk(session, chunk_size, count)

        bucket_start = bucket_end

    _commit_stage(session, chunk_size)
    return count


def _rebuild_hourly_statistics(
    session: Session,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int | None = None,
) -> int:
    """Rebuild hourly measurement statistics from short-term statistics."""
    rows = session.scalars(
        select(StatisticsShortTerm)
        .where(
            StatisticsShortTerm.metadata_id == statistic_metadata_id,
            StatisticsShortTerm.start_ts >= start,
            StatisticsShortTerm.start_ts < end,
        )
        .order_by(StatisticsShortTerm.start_ts.asc())
    ).all()

    by_hour: dict[float, list[tuple[float | None, float | None, float | None]]] = {}
    for row in rows:
        row_start_ts = row.start_ts
        if row_start_ts is None:
            raise ValueError("Recorder statistic has no timestamp")
        hour = _floor_period(row_start_ts, LONG_TERM_SECONDS)
        by_hour.setdefault(hour, []).append((row.mean, row.min, row.max))

    created_ts = datetime.now(UTC).timestamp()
    count = 0
    hour = start
    while hour < end:
        hour_rows = by_hour.get(hour, [])
        means = [row[0] for row in hour_rows if row[0] is not None]
        mins = [row[1] for row in hour_rows if row[1] is not None]
        maxs = [row[2] for row in hour_rows if row[2] is not None]

        if means and mins and maxs:
            stats: StatisticDataTimestamp = {
                "start_ts": hour,
                "mean": sum(means) / len(means),
                "min": min(mins),
                "max": max(maxs),
            }
            session.add(
                Statistics.from_stats_ts(
                    statistic_metadata_id,
                    stats,
                    created_ts,
                )
            )
            count += 1
            _commit_insert_chunk(session, chunk_size, count)

        hour += LONG_TERM_SECONDS

    _commit_stage(session, chunk_size)
    return count


def _load_states_for_statistics(
    session: Session,
    metadata_id: int,
    start: float,
    end: float,
) -> list[tuple[States, str | None]]:
    """Load the state active at start plus changes through end."""
    predecessor = session.execute(
        select(States, StateAttributes.shared_attrs, States.attributes)
        .outerjoin(
            StateAttributes,
            States.attributes_id == StateAttributes.attributes_id,
        )
        .where(
            States.metadata_id == metadata_id,
            States.last_updated_ts <= start,
        )
        .order_by(States.last_updated_ts.desc(), States.state_id.desc())
        .limit(1)
    ).first()

    rows = session.execute(
        select(States, StateAttributes.shared_attrs, States.attributes)
        .outerjoin(
            StateAttributes,
            States.attributes_id == StateAttributes.attributes_id,
        )
        .where(
            States.metadata_id == metadata_id,
            States.last_updated_ts > start,
            States.last_updated_ts < end,
        )
        .order_by(States.last_updated_ts.asc(), States.state_id.asc())
    ).all()

    result: list[tuple[States, str | None]] = []
    if predecessor is not None:
        result.append((predecessor[0], predecessor[1] or predecessor[2]))
    result.extend((row[0], row[1] or row[2]) for row in rows)
    return result

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Simple helper functions
#--------------------------------------------------------------------------------
def _floor_period(timestamp: float, period: int) -> float:
    return float(math.floor(timestamp / period) * period)


def _ceil_period(timestamp: float, period: int) -> float:
    floor = _floor_period(timestamp, period)
    return floor if timestamp == floor else floor + period

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder cache handling
#--------------------------------------------------------------------------------
def _refresh_recorder_caches(
    recorder_instance,
    entity_id: str,
    statistic_metadata_id: int,
) -> None:
    """Refresh Recorder caches after a committed history import."""

    recorder_instance.states_manager.pop_committed(entity_id)

    with session_scope(
        session=recorder_instance.get_session(),
        read_only=True,
    ) as session:
        metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(StatesMeta.entity_id == entity_id)
        )

        latest_state: States | None = None
        if metadata_id is not None:
            latest_state = session.scalar(
                select(States)
                .where(States.metadata_id == metadata_id)
                .order_by(
                    States.last_updated_ts.desc(),
                    States.state_id.desc(),
                )
                .limit(1)
            )

        if latest_state is not None:
            cache_state = States(
                state_id=latest_state.state_id,
                last_updated_ts=latest_state.last_updated_ts,
            )
            recorder_instance.states_manager.add_pending(entity_id, cache_state)
            recorder_instance.states_manager.post_commit_pending()

        recorder_instance.states_meta_manager.get(
            entity_id,
            session,
            True,
        )
        recorder_instance.states_manager.load_from_db(session)

    run_cache = recorder_statistics.get_short_term_statistics_run_cache(
        recorder_instance.hass
    )
    latest_ids = getattr(run_cache, "_latest_id_by_metadata_id", None)
    if latest_ids is not None:
        latest_ids.pop(statistic_metadata_id, None)

    with session_scope(
        session=recorder_instance.get_session(),
        read_only=True,
    ) as session:
        recorder_statistics.cache_latest_short_term_statistic_id_for_metadata_id(
            run_cache,
            session,
            statistic_metadata_id,
        )


def recover_recorder_caches_after_failure(instance, entity_id: str) -> None:
    """Restore Recorder caches after a failed history operation."""
    instance.states_manager.pop_committed(entity_id)

    with session_scope(session=instance.get_session(), read_only=True) as session:
        metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(StatesMeta.entity_id == entity_id)
        )
        latest_state = None
        if metadata_id is not None:
            latest_state = session.scalar(
                select(States)
                .where(States.metadata_id == metadata_id)
                .order_by(States.last_updated_ts.desc(), States.state_id.desc())
                .limit(1)
            )

        if latest_state is not None:
            cache_state = States(
                state_id=latest_state.state_id,
                last_updated_ts=latest_state.last_updated_ts,
            )
            instance.states_manager.add_pending(entity_id, cache_state)
            instance.states_manager.post_commit_pending()

        instance.states_manager.load_from_db(session)
        instance.states_meta_manager.get(entity_id, session, True)

        clear_cache = getattr(instance.statistics_meta_manager, "_clear_cache", None)
        if clear_cache is not None:
            clear_cache([entity_id])
        instance.statistics_meta_manager.get_many(
            session,
            statistic_ids={entity_id},
        )


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _get_or_create_attributes_id(
    session: Session,
    attributes: dict[str, Any],
) -> int | None:
    """Reuse or create the normalized Recorder attributes row."""
    if not attributes:
        return None

    shared_attrs = JSON_DUMP(attributes)
    shared_attrs_bytes = shared_attrs.encode("utf-8")
    data_hash = StateAttributes.hash_shared_attrs_bytes(shared_attrs_bytes)

    attributes_id = session.scalar(
        select(StateAttributes.attributes_id).where(
            StateAttributes.hash == data_hash,
            StateAttributes.shared_attrs == shared_attrs,
        )
    )
    if attributes_id is not None:
        return attributes_id

    db_attrs = StateAttributes(hash=data_hash, shared_attrs=shared_attrs)
    session.add(db_attrs)
    session.flush()
    return db_attrs.attributes_id

#endregion
#--------------------------------------------------------------------------------
def _validate_chunk_size(chunk_size: int | None) -> None:
    if chunk_size is not None and chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")


def _commit_insert_chunk(
    session: Session,
    chunk_size: int | None,
    count: int,
) -> None:
    if chunk_size is not None and count % chunk_size == 0:
        session.flush()
        session.commit()
        _LOGGER.info(
            "Committed insert chunk; %d rows processed",
            count,
        )

def _commit_stage(session: Session, chunk_size: int | None) -> None:
    if chunk_size is not None:
        session.flush()
        session.commit()
        _LOGGER.info("Committed end of database stage")

# Backwards-compatible public names used by earlier task/maintenance revisions.
delete_states = _delete_states
rebuild_statistics = _rebuild_statistics
refresh_recorder_caches = _refresh_recorder_caches
