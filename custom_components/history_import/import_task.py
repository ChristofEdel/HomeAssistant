"""Recorder task scaffolding and request handling for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
from zoneinfo import ZoneInfo
from functools import partial

from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.recorder import get_instance

from .csv_reader import Sample, read_and_validate_csv, resolve_file_path
from .importer import (
    ImportMode,
    perform_import,
    recover_recorder_caches_after_failure,
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------

class HistoryImportError(HomeAssistantError):
    """Base class for history import errors."""


#--------------------------------------------------------------------------------
#region Actual import task, runs perform_import
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class HistoryImportRecorderTask(RecorderTask):
    """Run one history import atomically on the Recorder thread."""

    entity_id: str
    attributes: dict[str, Any]
    samples: tuple[Sample, ...]
    mode: ImportMode
    future: asyncio.Future[dict[str, Any]]

    def run(self, instance) -> None:  # type: ignore[override]
        """Execute the import on the Recorder thread."""
        try:
            result = perform_import(
                instance,
                self.entity_id,
                self.attributes,
                self.samples,
                self.mode,
            ).as_dict()
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

def _set_future_result(future: asyncio.Future, result: dict[str, Any]) -> None:
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
) -> dict[str, Any]:
    """Validate an import request, queue it on Recorder, and return the result."""

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

    # validate the file path
    file_path = resolve_file_path(hass, file_name)
    local_tz = ZoneInfo(hass.config.time_zone)

    # Read the CSV and return all individual samples that we want to add
    samples = await hass.async_add_executor_job(
        read_and_validate_csv,
        file_path,
        time_zone,
        local_tz,
    )

    # Import them into the database
    instance = get_instance(hass)
    future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    instance.queue_task(
        HistoryImportRecorderTask(
            entity_id  = entity_id,
            attributes = dict(entity_state.attributes),
            samples    = samples,
            mode       = mode,
            future     = future,
        )
    )

    try:
        return await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryImportError(f"History import failed: {err}") from err

#endregion
#--------------------------------------------------------------------------------
