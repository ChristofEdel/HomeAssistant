"""Tasks for processing the recalculation of an integration sensor"""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
import logging
from typing import Any

from homeassistant.components.integration.const import (
    METHOD_LEFT,
    METHOD_RIGHT,
    METHOD_TRAPEZOIDAL,
)
from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.recorder import get_instance

from ..recorder_handling import recover_recorder_caches_after_failure
from .rerun_integration import (
    ReintegrationPlan,
    ReintegrationResult,
    perform_rerun_integration,
    prepare_rerun_integration,
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

_INTEGRATION_METHOD_BY_CLASS_NAME = {
    "_Left": METHOD_LEFT,
    "_Right": METHOD_RIGHT,
    "_Trapezoidal": METHOD_TRAPEZOIDAL,
}


class HistoryMaintenanceError(HomeAssistantError):
    """Base class for history maintenance errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_rerun_integration_sensor, called by the UI (via handle_rerun_integration())
#--------------------------------------------------------------------------------

async def async_rerun_integration_sensor(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Validate an Integral replay request, queue it on Recorder, and return the result."""

    # Validate the target entity and resolve the live Integral sensor object. The
    # live object is the authoritative source for the effective Integral settings,
    # whether the sensor was configured through YAML or a config entry.
    target_state = hass.states.get(entity_id)
    if target_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if target_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")

    sensor_component = hass.data.get(DATA_INSTANCES, {}).get("sensor")
    integration_entity = (
        sensor_component.get_entity(entity_id)
        if sensor_component is not None
        else None
    )
    if not isinstance(integration_entity, IntegrationSensor):
        raise ServiceValidationError(
            f"Entity must be an Integral integration sensor: {entity_id}"
        )

    source_entity_id = getattr(integration_entity, "_source_entity")
    if source_entity_id == entity_id:
        raise ServiceValidationError(
            f"Integral source and target sensors must be different: {entity_id}"
        )

    source_state = hass.states.get(source_entity_id)
    if source_state is None:
        raise ServiceValidationError(
            f"Integral source entity does not exist: {source_entity_id}"
        )

    method_object = getattr(integration_entity, "_method")
    method = _INTEGRATION_METHOD_BY_CLASS_NAME.get(type(method_object).__name__)
    if method is None:
        raise ServiceValidationError(
            f"Unsupported Integral method for {entity_id}: "
            f"{type(method_object).__name__}"
        )

    round_digits = getattr(integration_entity, "_round_digits")
    unit_prefix = int(getattr(integration_entity, "_unit_prefix"))
    unit_time = int(getattr(integration_entity, "_unit_time"))
    max_sub_interval_value = getattr(integration_entity, "_max_sub_interval")
    max_sub_interval = (
        max_sub_interval_value.total_seconds()
        if max_sub_interval_value is not None
        else None
    )

    statistic_ids = await hass.async_add_executor_job(
        partial(
            sensor_recorder.list_statistic_ids,
            hass,
            statistic_ids=[entity_id],
        )
    )

    target_statistics_metadata = statistic_ids.get(entity_id)
    if target_statistics_metadata is None:
        raise ServiceValidationError(
            f"No Recorder statistics metadata is available for {entity_id}"
        )

    # Calculate the rebuilt value first without modifying the target history. The
    # live accumulator continues to run while Recorder prepares the historical replay.
    replay_end_timestamp = datetime.now(UTC).timestamp()
    initial_internal_state = getattr(integration_entity, "_state")
    instance = get_instance(hass)
    prepare_future: asyncio.Future[ReintegrationPlan] = hass.loop.create_future()
    instance.queue_task(
        PrepareRerunIntegrationRecorderTask(
            source_entity_id     = source_entity_id,
            method               = method,
            round_digits         = round_digits,
            unit_prefix          = unit_prefix,
            unit_time            = unit_time,
            max_sub_interval     = max_sub_interval,
            replay_end_timestamp = replay_end_timestamp,
            chunk_size           = chunk_size,
            future               = prepare_future,
        )
    )

    try:
        plan = await prepare_future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Historical reintegration failed: {err}"
        ) from err

    # Rebase the running Integral sensor immediately. Any source changes which occurred
    # while the historical replay was being calculated are retained as an accumulator
    # delta, so all writes generated from this point use the corrected value.
    _rebase_live_integration_sensor(
        hass,
        integration_entity,
        source_entity_id,
        initial_internal_state,
        plan,
    )

    # Flush state-event callbacks which may still contain values generated before the
    # live accumulator was corrected. Their Recorder tasks are therefore guaranteed to
    # be ahead of the destructive rebuild task and will be deleted by that rebuild.
    await hass.async_block_till_done()

    current_internal_state_value = getattr(integration_entity, "_state")
    current_internal_state = (
        str(current_internal_state_value)
        if isinstance(current_internal_state_value, Decimal)
        else None
    )
    current_source_state = hass.states.get(source_entity_id)
    current_available = (
        current_source_state is None
        or current_source_state.state != STATE_UNAVAILABLE
    )
    current_state_timestamp = datetime.now(UTC).timestamp()

    # Queue the destructive rebuild before publishing the corrected current state.
    # Recorder therefore processes all pre-correction writes first, rebuilds the
    # database and cache around a corrected current anchor, and only then records this
    # current state and any later live updates.
    future: asyncio.Future[ReintegrationResult] = hass.loop.create_future()
    instance.queue_task(
        RerunIntegrationRecorderTask(
            target_entity_id           = entity_id,
            target_attributes          = dict(target_state.attributes),
            target_statistics_metadata = dict(target_statistics_metadata),
            round_digits               = round_digits,
            replay_end_timestamp       = replay_end_timestamp,
            plan                       = plan,
            current_internal_state     = current_internal_state,
            current_available          = current_available,
            current_state_timestamp    = current_state_timestamp,
            chunk_size                 = chunk_size,
            future                     = future,
        )
    )
    integration_entity.async_write_ha_state()

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Historical reintegration failed: {err}"
        ) from err

    return result.response


def _rebase_live_integration_sensor(
    hass: HomeAssistant,
    integration_entity: IntegrationSensor,
    source_entity_id: str,
    initial_internal_state: Decimal | None,
    plan: ReintegrationPlan,
) -> None:
    """Rebase the running Integral accumulator while preserving subsequent deltas."""

    if plan.final_internal_state is None:
        rebuilt_internal_state = None
    else:
        try:
            rebuilt_internal_state = Decimal(plan.final_internal_state)
        except InvalidOperation as err:
            raise HistoryMaintenanceError(
                f"Invalid rebuilt Integral state: {plan.final_internal_state}"
            ) from err

    current_internal_state = getattr(integration_entity, "_state")
    if isinstance(current_internal_state, Decimal):
        delta = (
            current_internal_state - initial_internal_state
            if isinstance(initial_internal_state, Decimal)
            else current_internal_state
        )
        internal_state = (
            rebuilt_internal_state + delta
            if rebuilt_internal_state is not None
            else delta
        )
    else:
        internal_state = rebuilt_internal_state

    setattr(integration_entity, "_state", internal_state)
    setattr(integration_entity, "_last_valid_state", internal_state)

    current_source_state = hass.states.get(source_entity_id)
    available = (
        current_source_state is None
        or current_source_state.state != STATE_UNAVAILABLE
    )
    setattr(integration_entity, "_attr_available", available)

    if current_source_state is not None and available:
        derive_attributes = getattr(
            integration_entity,
            "_derive_and_set_attributes_from_state",
        )
        derive_attributes(current_source_state)


#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder tasks
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class PrepareRerunIntegrationRecorderTask(RecorderTask):
    """Prepare an Integral replay on the Recorder thread without modifying history."""

    source_entity_id: str
    method: str
    round_digits: int | None
    unit_prefix: int
    unit_time: int
    max_sub_interval: float | None
    replay_end_timestamp: float
    chunk_size: int
    future: asyncio.Future[ReintegrationPlan]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = prepare_rerun_integration(
                instance,
                self.source_entity_id,
                self.method,
                self.round_digits,
                self.unit_prefix,
                self.unit_time,
                self.max_sub_interval,
                self.replay_end_timestamp,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - propagate to caller
            _LOGGER.exception(
                "Historical reintegration preparation failed for %s",
                self.source_entity_id,
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


@dataclass(slots=True)
class RerunIntegrationRecorderTask(RecorderTask):
    """Historically replay an Integral sensor on the Recorder thread."""

    target_entity_id: str
    target_attributes: dict[str, Any]
    target_statistics_metadata: dict[str, Any]
    round_digits: int | None
    replay_end_timestamp: float
    plan: ReintegrationPlan
    current_internal_state: str | None
    current_available: bool
    current_state_timestamp: float
    chunk_size: int
    future: asyncio.Future[ReintegrationResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_rerun_integration(
                instance,
                self.target_entity_id,
                self.target_attributes,
                self.target_statistics_metadata,
                self.round_digits,
                self.replay_end_timestamp,
                self.plan,
                self.current_internal_state,
                self.current_available,
                self.current_state_timestamp,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "Historical reintegration failed for %s",
                self.target_entity_id,
            )
            try:
                recover_recorder_caches_after_failure(
                    instance,
                    self.target_entity_id,
                )
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "Historical reintegration failed and Recorder cache recovery also failed"
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

