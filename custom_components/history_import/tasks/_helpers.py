#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from enum import StrEnum
import math
import asyncio
from datetime import UTC, datetime
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import delete, func, select, update
from homeassistant.components.recorder.db_schema import StatesMeta, StateAttributes
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.helpers.json import JSON_DUMP

#endregion
#--------------------------------------------------------------------------------


class ImportMode(StrEnum):
    APPEND = "append"
    OVERWRITE = "overwrite"
    REPLACE = "replace"

#--------------------------------------------------------------------------------
#region Attributes Metadata helpers
#--------------------------------------------------------------------------------

def get_or_create_attributes_id(
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


#--------------------------------------------------------------------------------
#region States Metadata helpers
#--------------------------------------------------------------------------------

def get_states_metadata_id(
    session: Session,
    entity_id: str,
) -> int | None:
    """Get or create the Recorder states metadata row for an entity."""
    return session.scalar(
        select(StatesMeta.metadata_id).where(
            StatesMeta.entity_id == entity_id
        )
    )   

def get_or_create_states_metadata_id(
    session: Session,
    entity_id: str,
) -> int:
    """Return the states metadata id for an entity, creating it if needed."""
    states_metadata = session.scalar(
        select(StatesMeta).where(StatesMeta.entity_id == entity_id)
    )
    if states_metadata is None:
        states_metadata = StatesMeta(entity_id=entity_id)
        session.add(states_metadata)
        session.flush()

    assert states_metadata.metadata_id is not None
    return states_metadata.metadata_id

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Statistics Metadata helpers
#--------------------------------------------------------------------------------

def get_statistics_metadata_id(
    recorderInstance,
    session: Session,
    entity_id: str,
) -> int | None:
    query_result = recorderInstance.statistics_meta_manager.get(
        session,
        entity_id,
    )
    if query_result:
        statistics_metadata_id, _ = query_result
        return statistics_metadata_id
    return None

def get_or_create_statistics_metadata_id(
    recorderInstance,
    session: Session,
    entity_id: str,
    statistics_metadata: dict[str, Any] | None = None,
) -> int:
    """Return statistics metadata id for an entity, creating it if needed."""
    return get_or_create_statistics_metadata(
        recorderInstance,
        session,
        entity_id,
        statistics_metadata,
    )[0]

def get_or_create_statistics_metadata(
    recorder_instance,
    session: Session,
    entity_id: str,
    statistics_metadata: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return statistics metadata for an entity, creating it if needed."""

    # Check if statistics metadata already exists for the entity, and return it if so
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

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Simple helper functions
#--------------------------------------------------------------------------------

def floor_period(timestamp: float, period: int) -> float:
    return float(math.floor(timestamp / period) * period)

def ceil_period(timestamp: float, period: int) -> float:
    floor = floor_period(timestamp, period)
    return floor if timestamp == floor else floor + period

def format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()

def set_future_exception(future: asyncio.Future, err: Exception) -> None:
    if not future.done():
        future.set_exception(err)


def set_future_result(future: asyncio.Future, result: Any) -> None:
    if not future.done():
        future.set_result(result)


#endregion
#--------------------------------------------------------------------------------
