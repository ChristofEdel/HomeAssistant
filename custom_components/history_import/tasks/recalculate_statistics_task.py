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

from ._helpers import set_future_exception, set_future_result
from ..recorder_handling import refresh_recorder_caches, recover_recorder_caches_after_failure
from .recalculate_statistics import RecalculateResult, perform_recalculate
_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

class HistoryMaintenanceError(HomeAssistantError):
    """Base class for history maintenance errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder tasks
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class RecalculateRecorderTask(RecorderTask):
    """Recalculate all measurement statistics on the Recorder thread."""

    entity_id: str
    statistics_metadata: dict[str, Any]
    chunk_size: int
    future: asyncio.Future[RecalculateResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_recalculate(
                instance,
                self.entity_id,
                self.statistics_metadata,
                self.chunk_size,
            )
            try:
                refresh_recorder_caches(
                    instance,
                    self.entity_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Statistics recalculation committed, but Recorder cache refresh failed"
                )

        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "Statistics recalculation failed for %s",
                self.entity_id,
            )
            try:
                recover_recorder_caches_after_failure(instance, self.entity_id)
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "Statistics recalculation failed and Recorder cache recovery also failed"
                )
            instance.hass.loop.call_soon_threadsafe(
                set_future_exception,
                self.future,
                err,
            )
        else:
            instance.hass.loop.call_soon_threadsafe(
                set_future_result,
                self.future,
                result,
            )



#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_recalculate_statistics, called by the UI (via handle_recalculate())
#--------------------------------------------------------------------------------

async def async_recalculate_statistics(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Validate a recalculation request, queue it on Recorder, and return the result."""

    # Validate the entity
    entity_state = hass.states.get(entity_id)
    if entity_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if entity_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")
    if entity_state.attributes.get("state_class") != SensorStateClass.MEASUREMENT:
        raise ServiceValidationError(
            f"Entity must have state_class measurement: {entity_id}"
        )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[entity_id],
        )
    )

    recorder_statistics_metadata = statistic_ids.get(entity_id)
    if recorder_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {entity_id}"
        )

    # Recalculate statistics in the database
    instance = get_instance(hass)
    future: asyncio.Future[RecalculateResult] = hass.loop.create_future()
    instance.queue_task(
        RecalculateRecorderTask(
            entity_id            = entity_id,
            statistics_metadata = dict(recorder_statistics_metadata),
            chunk_size           = chunk_size,
            future               = future,
        )
    )

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Statistics recalculation failed: {err}"
        ) from err
    
    return result.as_dict()

#endregion
#--------------------------------------------------------------------------------


