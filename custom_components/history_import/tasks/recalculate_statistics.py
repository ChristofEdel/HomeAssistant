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
from typing import Any, Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from homeassistant.components.recorder.db_schema import (
    States,
    StateAttributes,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticDataTimestamp
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.recorder import session_scope
import yaml
from ._helpers import (
    ImportMode,
    floor_period, ceil_period, 
    format_timestamp, 
    get_states_metadata_id,
    get_or_create_statistics_metadata_id
)

_LOGGER = logging.getLogger(__name__)

SHORT_TERM_SECONDS: Final = 5 * 60
LONG_TERM_SECONDS: Final = 60 * 60

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recalculate implementation
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class RecalculateResult:
    entity_id: str
    states_read: int = 0
    short_term_statistics_rebuilt: int = 0
    long_term_statistics_rebuilt: int = 0
    oldest_timestamp: float | None = None
    newest_timestamp: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "states_read": self.states_read,
            "short_term_statistics_rebuilt": self.short_term_statistics_rebuilt,
            "long_term_statistics_rebuilt": self.long_term_statistics_rebuilt,
            "oldest_timestamp": format_timestamp(self.oldest_timestamp),
            "newest_timestamp": format_timestamp(self.newest_timestamp),
        }

def perform_recalculate(
    recorderInstance,
    entity_id: str,
    state_class: SensorStateClass,
    statistics_metadata: dict[str, Any],
    chunk_size: int,
) -> RecalculateResult:
    """Delete all statistics and rebuild them from the complete state history."""

    result: RecalculateResult = RecalculateResult(entity_id=entity_id)

    with session_scope(session=recorderInstance.get_session()) as session:

        # Get or create statistics metadata
        states_metadata_id = get_states_metadata_id(session, entity_id)
        statistics_metadata_id = get_or_create_statistics_metadata_id(
            recorderInstance,
            session,
            entity_id,
            statistics_metadata,
        )

        if states_metadata_id is not None:
            result.states_read = session.scalar(
                select(func.count())
                .select_from(States)
                .where(States.metadata_id == states_metadata_id)
            ) or 0
            result.oldest_timestamp = session.scalar(
                select(func.min(States.last_updated_ts)).where(
                    States.metadata_id == states_metadata_id
                )
            )
            result.newest_timestamp = session.scalar(
                select(func.max(States.last_updated_ts)).where(
                    States.metadata_id == states_metadata_id
                )
            )

        if states_metadata_id is None or result.oldest_timestamp is None:
            delete_all_statistics(
                session,
                statistics_metadata_id
            )
            result.short_term_statistics_rebuilt = 0
            result.long_term_statistics_rebuilt = 0
            session.commit()
        else:
            # Re-use the importer's REPLACE statistics path. This deletes all
            # existing short/long-term statistics and rebuilds the complete range.
            rebuild_result = rebuild_statistics_with_commit(
                session,
                ImportMode.REPLACE,
                state_class             = state_class,
                rebuild_start_timestamp = result.oldest_timestamp,
                rebuild_end_timestamp   = None,
                states_metadata_id      = states_metadata_id,
                statistics_metadata_id  = statistics_metadata_id,
                chunk_size              = chunk_size,
            )
            result.short_term_statistics_rebuilt = rebuild_result["short_term"]
            result.long_term_statistics_rebuilt = rebuild_result["hourly"]

    # The database changes have committed successfully at this point
    _LOGGER.info(
        "Rebuild result:\n%s",
        yaml.safe_dump(result.as_dict(), sort_keys=False),
    )
    return result;

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region Supporting functions
#--------------------------------------------------------------------------------

def delete_all_statistics(
    session: Session,
    statistics_metadata_id: int
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
    return

def delete_statistics_range(
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

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Statistics rebuild function + helpers
#--------------------------------------------------------------------------------
def rebuild_statistics_with_commit(
    session: Session,
    mode: ImportMode,
    state_class: SensorStateClass,
    rebuild_start_timestamp: float,
    rebuild_end_timestamp: float | None,
    states_metadata_id: int,
    statistics_metadata_id: int,
    chunk_size: int,
) -> dict[str, int]:
    """Delete invalid statistics and rebuild them from Recorder states."""

    now_timestamp = datetime.now(UTC).timestamp()

    # Determine the first statistics buckets affected by the import
    rebuild_short_start = floor_period(rebuild_start_timestamp, SHORT_TERM_SECONDS)
    rebuild_hour_start = floor_period(rebuild_start_timestamp, LONG_TERM_SECONDS)

    # Statistics can only be generated for completed periods
    rebuild_short_end_exclusive = floor_period(now_timestamp, SHORT_TERM_SECONDS)
    rebuild_hour_end_exclusive = floor_period(now_timestamp, LONG_TERM_SECONDS)

    # Measurement buckets are independent, so a bounded import only invalidates
    # buckets through the transition back into retained history. For a cumulative
    # total_increasing sensor, changing an old value changes every later sum, so
    # the rebuild must continue through the newest completed bucket.
    if (
        state_class == SensorStateClass.MEASUREMENT
        and rebuild_end_timestamp is not None
    ):
        rebuild_short_end_exclusive = min(
            ceil_period(rebuild_end_timestamp, SHORT_TERM_SECONDS),
            rebuild_short_end_exclusive,
        )
        rebuild_hour_end_exclusive = min(
            ceil_period(rebuild_end_timestamp, LONG_TERM_SECONDS),
            rebuild_hour_end_exclusive,
        )

    # Remove statistics made invalid by the state changes.
    if state_class == SensorStateClass.TOTAL_INCREASING:
        if mode == ImportMode.APPEND:
            # Earlier imported history changes the cumulative sum from this point
            # onward, but statistics before the new history can remain as a baseline.
            delete_statistics_range(
                session,
                StatisticsShortTerm,
                statistics_metadata_id,
                rebuild_short_start,
                None,
                chunk_size,
            )
            delete_statistics_range(
                session,
                Statistics,
                statistics_metadata_id,
                rebuild_hour_start,
                None,
                chunk_size,
            )
        else:
            # OVERWRITE removes all states through the imported range and REPLACE
            # removes all states, so no earlier cumulative statistics remain valid.
            delete_all_statistics(session, statistics_metadata_id)
    else:
        match mode:
            case ImportMode.APPEND:
                # Only the imported range and the transition into the retained
                # existing history are affected.
                delete_statistics_range(
                    session,
                    StatisticsShortTerm,
                    statistics_metadata_id,
                    rebuild_short_start,
                    rebuild_short_end_exclusive,
                    chunk_size,
                )
                delete_statistics_range(
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
                delete_all_statistics(session, statistics_metadata_id)

    session.commit()
    _LOGGER.info("Deleted statistics for %s", statistics_metadata_id)

    # Rebuild 5-minute statistics
    short_term_statistics_rebuilt = 0
    if rebuild_short_end_exclusive > rebuild_short_start:
        short_term_statistics_rebuilt = _rebuild_short_term_statistics(
            session,
            state_class,
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
            state_class,
            statistics_metadata_id,
            rebuild_hour_start,
            rebuild_hour_end_exclusive,
            chunk_size,
        )

    return {
        "short_term": short_term_statistics_rebuilt,
        "hourly": hourly_statistics_rebuilt,
    }


@dataclass(slots=True)
class _StateBucket:
    start: float
    end: float
    active_value: float | None
    changes: list[tuple[float, float]]


@dataclass(slots=True)
class _TotalIncreasingAccumulator:
    state: float | None = None
    sum: float = 0.0

    def apply(self, value: float) -> None:
        """Apply one Recorder state using Home Assistant total_increasing semantics."""
        if self.state is None:
            self.state = value
            return

        # Home Assistant ignores negative values once a previous state exists.
        if value < 0:
            return

        # A drop below 90% of the previous value is a reset. The new counter
        # value is treated as consumption accumulated from zero after that reset.
        if value < 0.9 * self.state:
            self.sum += value
        else:
            self.sum += value - self.state

        self.state = value


def _iter_state_buckets(
    values: list[tuple[float, float]],
    start: float,
    end: float,
    period: int,
):
    """Yield state buckets without duplicating period traversal logic."""
    pointer = 0
    active_value: float | None = None

    # Keep the last value strictly before the first bucket. Changes exactly on
    # the boundary stay in the bucket so cumulative sensors can account for them.
    while pointer < len(values) and values[pointer][0] < start:
        active_value = values[pointer][1]
        pointer += 1

    bucket_start = start
    while bucket_start < end:
        bucket_end = bucket_start + period
        changes: list[tuple[float, float]] = []

        while pointer < len(values) and values[pointer][0] < bucket_end:
            changes.append(values[pointer])
            pointer += 1

        yield _StateBucket(
            start=bucket_start,
            end=bucket_end,
            active_value=active_value,
            changes=changes,
        )

        if changes:
            active_value = changes[-1][1]
        bucket_start = bucket_end


def _measurement_statistics_for_bucket(
    bucket: _StateBucket,
    *,
    first_bucket: bool,
) -> StatisticDataTimestamp | None:
    """Build one measurement statistics bucket."""
    active_value = bucket.active_value
    change_index = 0

    # The previous implementation loaded the latest state at or before the
    # overall rebuild start as its predecessor. Preserve that behavior for the
    # first bucket only. Later bucket-boundary changes are processed normally.
    if first_bucket:
        while (
            change_index < len(bucket.changes)
            and bucket.changes[change_index][0] <= bucket.start
        ):
            active_value = bucket.changes[change_index][1]
            change_index += 1

    min_value: float | None = active_value
    max_value: float | None = active_value
    weighted_total = 0.0
    weighted_seconds = 0.0
    cursor = bucket.start

    for timestamp, value in bucket.changes[change_index:]:
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

    if active_value is not None:
        duration = max(0.0, bucket.end - cursor)
        weighted_total += active_value * duration
        weighted_seconds += duration

    if min_value is None or max_value is None or weighted_seconds <= 0:
        return None

    return {
        "start_ts": bucket.start,
        "mean": weighted_total / weighted_seconds,
        "min": min_value,
        "max": max_value,
    }


def _load_total_increasing_baseline(
    session: Session,
    statistic_metadata_id: int,
    start: float,
) -> _TotalIncreasingAccumulator:
    """Load the retained cumulative baseline immediately before a rebuild."""
    row = session.scalar(
        select(StatisticsShortTerm)
        .where(
            StatisticsShortTerm.metadata_id == statistic_metadata_id,
            StatisticsShortTerm.start_ts < start,
        )
        .order_by(StatisticsShortTerm.start_ts.desc())
        .limit(1)
    )

    if row is None or row.state is None:
        return _TotalIncreasingAccumulator()

    return _TotalIncreasingAccumulator(
        state=row.state,
        sum=row.sum or 0.0,
    )


def _rebuild_short_term_statistics(
    session: Session,
    state_class: SensorStateClass,
    states_metadata_id: int,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int,
) -> int:
    """Rebuild complete 5-minute statistics buckets from Recorder states."""

    raw_states = _load_states_for_statistics(
        session,
        states_metadata_id,
        start,
        end,
    )

    values: list[tuple[float, float]] = []
    for db_state, _ in raw_states:
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

    total_increasing = None
    if state_class == SensorStateClass.TOTAL_INCREASING:
        total_increasing = _load_total_increasing_baseline(
            session,
            statistic_metadata_id,
            start,
        )
    elif state_class != SensorStateClass.MEASUREMENT:
        raise ValueError(f"Unsupported statistics state class: {state_class}")

    for bucket_index, bucket in enumerate(
        _iter_state_buckets(values, start, end, SHORT_TERM_SECONDS)
    ):
        stats: StatisticDataTimestamp | None

        if state_class == SensorStateClass.MEASUREMENT:
            stats = _measurement_statistics_for_bucket(
                bucket,
                first_bucket=bucket_index == 0,
            )
        else:
            assert total_increasing is not None

            # Reconcile the state active at the first boundary with a retained
            # baseline, if one exists, then apply all changes in this bucket.
            if bucket.active_value is not None:
                if total_increasing.state is None:
                    total_increasing.apply(bucket.active_value)
                elif bucket.active_value != total_increasing.state:
                    total_increasing.apply(bucket.active_value)

            for _, value in bucket.changes:
                total_increasing.apply(value)

            if total_increasing.state is None:
                stats = None
            else:
                stats = {
                    "start_ts": bucket.start,
                    "state": total_increasing.state,
                    "sum": total_increasing.sum,
                }

        if stats is not None:
            session.add(
                StatisticsShortTerm.from_stats_ts(
                    statistic_metadata_id,
                    stats,
                    created_ts,
                )
            )
            count += 1
            if count % chunk_size == 0:
                session.commit()

    session.commit()
    return count


def _rebuild_hourly_statistics(
    session: Session,
    state_class: SensorStateClass,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int,
) -> int:
    """Rebuild hourly statistics from short-term statistics."""
    rows = session.scalars(
        select(StatisticsShortTerm)
        .where(
            StatisticsShortTerm.metadata_id == statistic_metadata_id,
            StatisticsShortTerm.start_ts >= start,
            StatisticsShortTerm.start_ts < end,
        )
        .order_by(StatisticsShortTerm.start_ts.asc())
    ).all()

    by_hour: dict[float, list[StatisticsShortTerm]] = {}
    for row in rows:
        row_start_ts = row.start_ts
        if row_start_ts is None:
            raise ValueError("Recorder statistic has no timestamp")
        hour = floor_period(row_start_ts, LONG_TERM_SECONDS)
        by_hour.setdefault(hour, []).append(row)

    created_ts = datetime.now(UTC).timestamp()
    count = 0
    hour = start
    while hour < end:
        hour_rows = by_hour.get(hour, [])
        stats: StatisticDataTimestamp | None = None

        if state_class == SensorStateClass.MEASUREMENT:
            means = [row.mean for row in hour_rows if row.mean is not None]
            mins = [row.min for row in hour_rows if row.min is not None]
            maxs = [row.max for row in hour_rows if row.max is not None]

            if means and mins and maxs:
                stats = {
                    "start_ts": hour,
                    "mean": sum(means) / len(means),
                    "min": min(mins),
                    "max": max(maxs),
                }
        elif state_class == SensorStateClass.TOTAL_INCREASING:
            if hour_rows:
                last_row = hour_rows[-1]
                if last_row.state is not None and last_row.sum is not None:
                    stats = {
                        "start_ts": hour,
                        "state": last_row.state,
                        "sum": last_row.sum,
                    }
                    if last_row.last_reset_ts is not None:
                        stats["last_reset_ts"] = last_row.last_reset_ts
        else:
            raise ValueError(f"Unsupported statistics state class: {state_class}")

        if stats is not None:
            session.add(
                Statistics.from_stats_ts(
                    statistic_metadata_id,
                    stats,
                    created_ts,
                )
            )
            count += 1
            if count % chunk_size == 0:
                session.commit()

        hour += LONG_TERM_SECONDS

    session.commit()
    return count


def _load_states_for_statistics(
    session: Session,
    metadata_id: int,
    start: float,
    end: float,
) -> list[tuple[States, str | None]]:
    """Load the state active before start plus changes through end."""
    predecessor = session.execute(
        select(States, StateAttributes.shared_attrs, States.attributes)
        .outerjoin(
            StateAttributes,
            States.attributes_id == StateAttributes.attributes_id,
        )
        .where(
            States.metadata_id == metadata_id,
            States.last_updated_ts < start,
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
            States.last_updated_ts >= start,
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

