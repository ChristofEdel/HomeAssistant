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
    if rebuild_end_timestamp is not None:
        rebuild_short_end_exclusive = min(
            ceil_period(rebuild_end_timestamp, SHORT_TERM_SECONDS),
            rebuild_short_end_exclusive,
        )
        rebuild_hour_end_exclusive = min(
            ceil_period(rebuild_end_timestamp, LONG_TERM_SECONDS),
            rebuild_hour_end_exclusive,
        )

    # Remove statistics made invalid by the state changes
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

    session.commit()
    _LOGGER.info("Deleted statistics for %s", statistics_metadata_id)

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



def _rebuild_short_term_statistics(
    session: Session,
    states_metadata_id: int,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int,
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
            if count % chunk_size == 0:
                session.commit()

        bucket_start = bucket_end

    session.commit()
    return count


def _rebuild_hourly_statistics(
    session: Session,
    statistic_metadata_id: int,
    start: float,
    end: float,
    chunk_size: int,
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
        hour = floor_period(row_start_ts, LONG_TERM_SECONDS)
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

