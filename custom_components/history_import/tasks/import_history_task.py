"""Recorder task scaffolding and request handling for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any
from typing import cast
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from ..recorder_handling import refresh_recorder_caches, recover_recorder_caches_after_failure

from ..csv_reader import Sample, read_and_validate_csv
from .import_history import (
    ImportResult,
    perform_import
)
from ._helpers import ImportMode

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

class HistoryImportError(HomeAssistantError):
    """Base class for history import errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder task
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class ImportRecorderTask(RecorderTask):
    """Import historical states and rebuild statistics on the Recorder thread."""

    entity_id: str
    entity_attributes: dict[str, Any]
    state_class: SensorStateClass
    samples: tuple[Sample, ...]
    mode: ImportMode
    chunk_size: int
    future: asyncio.Future[ImportResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_import(
                instance,
                self.entity_id,
                self.entity_attributes,
                self.state_class,
                self.samples,
                self.mode,
                self.chunk_size,
            )
            try:
                refresh_recorder_caches(
                    instance,
                    self.entity_id
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "History import committed, but Recorder cache refresh failed"
                )

        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "History import failed for %s",
                self.entity_id,
            )
            try:
                recover_recorder_caches_after_failure(instance, self.entity_id)
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "History import failed and Recorder cache recovery also failed"
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


def _set_future_result(future: asyncio.Future, result: ImportResult) -> None:
    if not future.done():
        future.set_result(result)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_import_history, called by the UI (via handle_import())
#--------------------------------------------------------------------------------

async def async_import_history(
    hass: HomeAssistant,
    *,
    entity_id: str,
    file_name: str,
    mode: ImportMode,
    time_zone: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Validate an import request, queue it on Recorder, and return the result."""

    # Validate the entity
    entity_state = hass.states.get(entity_id)
    if entity_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if entity_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")
    state_class_value = entity_state.attributes.get("state_class")
    if state_class_value not in (
        SensorStateClass.MEASUREMENT,
        SensorStateClass.TOTAL_INCREASING,
    ):
        raise ServiceValidationError(
            f"Entity must have state_class measurement or total_increasing: {entity_id}"
        )
    state_class = SensorStateClass(state_class_value)

    # Read and validate the complete CSV before queueing any database work.
    file_path = Path(hass.config.config_dir) / file_name
    samples = await hass.async_add_executor_job(
        read_and_validate_csv,
        file_path,
        time_zone,
        cast(ZoneInfo, dt_util.DEFAULT_TIME_ZONE),
    )

    # Import state history and statistics in the database
    instance = get_instance(hass)
    future: asyncio.Future[ImportResult] = hass.loop.create_future()
    instance.queue_task(
        ImportRecorderTask(
            entity_id         = entity_id,
            entity_attributes = dict(entity_state.attributes),
            state_class       = state_class,
            samples           = samples,
            mode              = mode,
            chunk_size        = chunk_size,
            future            = future,
        )
    )

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryImportError(f"History import failed: {err}") from err

    return result.as_dict()

#endregion
#--------------------------------------------------------------------------------
