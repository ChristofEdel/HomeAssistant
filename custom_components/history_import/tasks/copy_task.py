"""Recorder maintenance task scaffolding and request handling for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import logging
from typing import Any

from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from ..recorder_handling import refresh_recorder_caches, recover_recorder_caches_after_failure

from .copy import CopyResult, perform_copy

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

_ALLOWED_COPY_STATE_CLASSES = {
    SensorStateClass.MEASUREMENT,
    SensorStateClass.TOTAL,
    SensorStateClass.TOTAL_INCREASING,
}

class HistoryMaintenanceError(HomeAssistantError):
    """Base class for history maintenance errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_copy_sensor_records, called by the UI (via handle_copy())
#--------------------------------------------------------------------------------

async def async_copy_sensor_records(
    hass: HomeAssistant,
    *,
    source_entity_id: str,
    target_entity_id: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Validate a copy request, queue it on Recorder, and return the result."""

    if source_entity_id == target_entity_id:
        raise ServiceValidationError("Source and target sensors must be different")

    # Validate both entities
    source_state = hass.states.get(source_entity_id)
    if source_state is None:
        raise ServiceValidationError(f"Entity does not exist: {source_entity_id}")

    target_state = hass.states.get(target_entity_id)
    if target_state is None:
        raise ServiceValidationError(f"Entity does not exist: {target_entity_id}")

    for entity_id, entity_state in (
        (source_entity_id, source_state),
        (target_entity_id, target_state),
    ):
        if entity_state.domain != "sensor":
            raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")
        if entity_state.attributes.get("state_class") not in _ALLOWED_COPY_STATE_CLASSES:
            raise ServiceValidationError(
                "Entity must have state_class measurement, total, or "
                f"total_increasing: {entity_id}"
            )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[target_entity_id],
        )
    )

    target_statistics_metadata = statistic_ids.get(target_entity_id)
    if target_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {target_entity_id}"
        )

    # Copy state history and statistics in the database
    instance = get_instance(hass)
    future: asyncio.Future[CopyResult] = hass.loop.create_future()
    instance.queue_task(
        CopyRecorderTask(
            source_entity_id           = source_entity_id,
            target_entity_id           = target_entity_id,
            target_statistics_metadata = dict(target_statistics_metadata),
            chunk_size                 = chunk_size,
            future                     = future,
        )
    )

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(f"History copy failed: {err}") from err

    return result.as_dict()

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder tasks
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class CopyRecorderTask(RecorderTask):
    """Copy complete Recorder history and statistics on the Recorder thread."""

    source_entity_id: str
    target_entity_id: str
    target_statistics_metadata: dict[str, Any]
    chunk_size: int
    future: asyncio.Future[CopyResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_copy(
                instance,
                self.source_entity_id,
                self.target_entity_id,
                self.target_statistics_metadata,
                self.chunk_size,
            )
            try:
                refresh_recorder_caches(
                    instance,
                    self.target_entity_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "History copy committed, but Recorder cache refresh failed"
                )

        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "History copy failed from %s to %s",
                self.source_entity_id,
                self.target_entity_id,
            )
            try:
                recover_recorder_caches_after_failure(instance, self.target_entity_id)
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "History copy failed and Recorder cache recovery also failed"
                )
            instance.hass.loop.call_soon_threadsafe(
                _set_future_exception,
                self.future,
                err,
            )
        else:
            instance.hass.loop.call_soon_threadsafe(
                _set_future_result,
                self.future,
                result,
            )


def _set_future_exception(future: asyncio.Future, err: Exception) -> None:
    if not future.done():
        future.set_exception(err)


def _set_future_result(future: asyncio.Future, result: Any) -> None:
    if not future.done():
        future.set_result(result)

#endregion
#--------------------------------------------------------------------------------
