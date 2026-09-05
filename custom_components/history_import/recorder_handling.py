#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    StatisticsMeta,
)
from homeassistant.helpers.json import JSON_DUMP
from homeassistant.helpers.recorder import session_scope

_LOGGER = logging.getLogger(__name__)

#endregion

def refresh_recorder_caches(
    recorder_instance,
    entity_id: str,
) -> None:
    """Refresh Recorder caches after a change to the history has been committed"""

    recorder_instance.states_manager.pop_committed(entity_id)

    statistic_metadata_id: int | None = None

    with session_scope(
        session=recorder_instance.get_session(),
        read_only=True,
    ) as session:
        metadata_id = session.scalar(
            select(StatesMeta.metadata_id).where(StatesMeta.entity_id == entity_id)
        )

        statistic_metadata_id = session.scalar(
            select(StatisticsMeta.id).where(StatisticsMeta.statistic_id == entity_id)
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

    if statistic_metadata_id is None:
        return

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

        statistic_metadata_id = session.scalar(
            select(StatisticsMeta.id).where(StatisticsMeta.statistic_id == entity_id)
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

        if statistic_metadata_id is not None:
            run_cache = recorder_statistics.get_short_term_statistics_run_cache(
                instance.hass
            )
            latest_ids = getattr(run_cache, "_latest_id_by_metadata_id", None)
            if latest_ids is not None:
                latest_ids.pop(statistic_metadata_id, None)
            recorder_statistics.cache_latest_short_term_statistic_id_for_metadata_id(
                run_cache,
                session,
                statistic_metadata_id,
            )
