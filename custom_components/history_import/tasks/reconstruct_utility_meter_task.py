"""Tasks for reconstructing a Utility Meter sensor."""

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

from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.components.utility_meter.const import (
    DAILY,
    HOURLY,
    MONTHLY,
    YEARLY,
)
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.recorder import get_instance

from ..recorder_handling import refresh_recorder_caches, recover_recorder_caches_after_failure
from .reconstruct_utility_meter import (
    UtilityMeterReconstructionPlan,
    UtilityMeterReconstructionResult,
    perform_reconstruct_utility_meter,
    prepare_reconstruct_utility_meter,
)

_LOGGER = logging.getLogger(__name__)

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Shared definitions
#--------------------------------------------------------------------------------

_ALLOWED_UTILITY_METER_PERIODS = {
    HOURLY,
    DAILY,
    MONTHLY,
    YEARLY,
}


class HistoryMaintenanceError(HomeAssistantError):
    """Base class for history maintenance errors."""

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region async_reconstruct_utility_meter, called by the UI
#--------------------------------------------------------------------------------

async def async_reconstruct_utility_meter(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Validate a Utility Meter replay, rebuild Recorder data, and return the result."""

    target_state = hass.states.get(entity_id)
    if target_state is None:
        raise ServiceValidationError(f"Entity does not exist: {entity_id}")
    if target_state.domain != "sensor":
        raise ServiceValidationError(f"Entity must be a sensor: {entity_id}")

    sensor_component = hass.data.get(DATA_INSTANCES, {}).get("sensor")
    utility_meter_entity = (
        sensor_component.get_entity(entity_id)
        if sensor_component is not None
        else None
    )
    if not isinstance(utility_meter_entity, UtilityMeterSensor):
        raise ServiceValidationError(
            f"Entity must be a Utility Meter sensor: {entity_id}"
        )

    meter_period = getattr(utility_meter_entity, "_period")
    if meter_period not in _ALLOWED_UTILITY_METER_PERIODS:
        raise ServiceValidationError(
            "Utility Meter period must be hourly, daily, monthly, or yearly: "
            f"{entity_id}"
        )

    if getattr(utility_meter_entity, "_tariff") is not None:
        raise ServiceValidationError(
            f"Tariff Utility Meter sensors are not supported: {entity_id}"
        )

    source_entity_id = getattr(utility_meter_entity, "_sensor_source_id")
    if source_entity_id == entity_id:
        raise ServiceValidationError(
            f"Utility Meter source and target sensors must be different: {entity_id}"
        )
    if hass.states.get(source_entity_id) is None:
        raise ServiceValidationError(
            f"Utility Meter source entity does not exist: {source_entity_id}"
        )

    cron_pattern = getattr(utility_meter_entity, "_cron_pattern")
    if not cron_pattern:
        raise ServiceValidationError(
            f"Utility Meter has no periodic reset schedule: {entity_id}"
        )

    state_class_value = target_state.attributes.get("state_class")
    if state_class_value not in (
        SensorStateClass.TOTAL,
        SensorStateClass.TOTAL_INCREASING,
    ):
        raise ServiceValidationError(
            f"Utility Meter must have state_class total or total_increasing: {entity_id}"
        )
    state_class = SensorStateClass(state_class_value)

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

    instance = get_instance(hass)
    plan: UtilityMeterReconstructionPlan | None = None
    initial_native_value: Decimal | None = None

    # If a scheduled meter reset occurs while Recorder is preparing the replay,
    # prepare again against the new live interval before touching the running meter.
    for _ in range(3):
        initial_native_value = _utility_meter_native_decimal(utility_meter_entity)
        initial_last_reset = getattr(utility_meter_entity, "_last_reset")
        replay_end_timestamp = datetime.now(UTC).timestamp()
        prepare_future: asyncio.Future[UtilityMeterReconstructionPlan] = (
            hass.loop.create_future()
        )
        instance.queue_task(
            PrepareUtilityMeterRecorderTask(
                source_entity_id         = source_entity_id,
                meter_period             = meter_period,
                cron_pattern             = cron_pattern,
                time_zone                = hass.config.time_zone,
                delta_values             = bool(
                    getattr(utility_meter_entity, "_sensor_delta_values")
                ),
                net_consumption          = bool(
                    getattr(utility_meter_entity, "_sensor_net_consumption")
                ),
                periodically_resetting   = bool(
                    getattr(utility_meter_entity, "_sensor_periodically_resetting")
                ),
                sensor_always_available  = bool(
                    getattr(utility_meter_entity, "_sensor_always_available")
                ),
                replay_end_timestamp     = replay_end_timestamp,
                chunk_size               = chunk_size,
                future                   = prepare_future,
            )
        )

        try:
            plan = await prepare_future
        except ServiceValidationError:
            raise
        except Exception as err:
            raise HistoryMaintenanceError(
                f"Utility Meter reconstruction failed: {err}"
            ) from err

        if getattr(utility_meter_entity, "_last_reset") == initial_last_reset:
            break
    else:
        raise HistoryMaintenanceError(
            "Utility Meter reset repeatedly while reconstruction was being prepared"
        )

    assert plan is not None
    _rebase_live_utility_meter_sensor(
        utility_meter_entity,
        initial_native_value,
        plan,
    )

    # Flush Recorder writes generated before the live meter was corrected. They are
    # guaranteed to be ahead of the destructive rebuild task and will be deleted.
    await hass.async_block_till_done()

    current_native_value = _utility_meter_native_decimal(utility_meter_entity)
    current_state = (
        STATE_UNAVAILABLE
        if not utility_meter_entity.available
        else STATE_UNKNOWN
        if current_native_value is None
        else str(current_native_value)
    )
    current_state_timestamp = datetime.now(UTC).timestamp()
    current_attributes = _current_utility_meter_attributes(
        target_state.attributes,
        utility_meter_entity,
    )

    future: asyncio.Future[UtilityMeterReconstructionResult] = hass.loop.create_future()
    instance.queue_task(
        ReconstructUtilityMeterRecorderTask(
            target_entity_id           = entity_id,
            target_attributes          = dict(target_state.attributes),
            current_attributes         = current_attributes,
            target_statistics_metadata = dict(target_statistics_metadata),
            state_class                = state_class,
            replay_end_timestamp       = replay_end_timestamp,
            plan                       = plan,
            current_state              = current_state,
            current_state_timestamp    = current_state_timestamp,
            chunk_size                 = chunk_size,
            future                     = future,
        )
    )
    utility_meter_entity.async_write_ha_state()

    try:
        result = await future
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HistoryMaintenanceError(
            f"Utility Meter reconstruction failed: {err}"
        ) from err

    return result.as_dict()


def _rebase_live_utility_meter_sensor(
    utility_meter_entity: UtilityMeterSensor,
    initial_native_value: Decimal | None,
    plan: UtilityMeterReconstructionPlan,
) -> None:
    """Rebase the running Utility Meter while preserving subsequent live deltas."""

    rebuilt_native_value = (
        Decimal(plan.final_native_value)
        if plan.final_native_value is not None
        else None
    )
    current_native_value = _utility_meter_native_decimal(utility_meter_entity)

    if current_native_value is not None:
        delta = (
            current_native_value - initial_native_value
            if initial_native_value is not None
            else current_native_value
        )
        native_value = (
            rebuilt_native_value + delta
            if rebuilt_native_value is not None
            else delta
        )
    else:
        native_value = rebuilt_native_value

    setattr(utility_meter_entity, "_attr_native_value", native_value)
    setattr(
        utility_meter_entity,
        "_last_period",
        Decimal(plan.final_last_period),
    )
    setattr(
        utility_meter_entity,
        "_last_reset",
        datetime.fromtimestamp(plan.final_last_reset_ts, UTC),
    )


def _utility_meter_native_decimal(
    utility_meter_entity: UtilityMeterSensor,
) -> Decimal | None:
    """Return the running Utility Meter value as Decimal."""

    native_value = utility_meter_entity.native_value
    if native_value is None:
        return None
    try:
        return Decimal(str(native_value))
    except (InvalidOperation, TypeError):
        return None


def _current_utility_meter_attributes(
    target_attributes: dict[str, Any],
    utility_meter_entity: UtilityMeterSensor,
) -> dict[str, Any]:
    """Return current Recorder attributes after rebasing the live Utility Meter."""

    attributes = dict(target_attributes)
    attributes.pop("next_reset", None)
    for key, value in utility_meter_entity.extra_state_attributes.items():
        if key != "next_reset":
            attributes[key] = value
    return attributes

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Recorder tasks
#--------------------------------------------------------------------------------

@dataclass(slots=True)
class PrepareUtilityMeterRecorderTask(RecorderTask):
    """Prepare a Utility Meter replay without modifying history."""

    source_entity_id: str
    meter_period: str
    cron_pattern: str
    time_zone: str
    delta_values: bool
    net_consumption: bool
    periodically_resetting: bool
    sensor_always_available: bool
    replay_end_timestamp: float
    chunk_size: int
    future: asyncio.Future[UtilityMeterReconstructionPlan]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = prepare_reconstruct_utility_meter(
                instance,
                self.source_entity_id,
                self.meter_period,
                self.cron_pattern,
                self.time_zone,
                self.delta_values,
                self.net_consumption,
                self.periodically_resetting,
                self.sensor_always_available,
                self.replay_end_timestamp,
                self.chunk_size,
            )
        except Exception as err:  # noqa: BLE001 - propagate to caller
            _LOGGER.exception(
                "Utility Meter reconstruction preparation failed for %s",
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
class ReconstructUtilityMeterRecorderTask(RecorderTask):
    """Reconstruct Utility Meter history and statistics on the Recorder thread."""

    target_entity_id: str
    target_attributes: dict[str, Any]
    current_attributes: dict[str, Any]
    target_statistics_metadata: dict[str, Any]
    state_class: SensorStateClass
    replay_end_timestamp: float
    plan: UtilityMeterReconstructionPlan
    current_state: str
    current_state_timestamp: float
    chunk_size: int
    future: asyncio.Future[UtilityMeterReconstructionResult]

    def run(self, instance) -> None:  # type: ignore[override]
        try:
            result = perform_reconstruct_utility_meter(
                instance,
                self.target_entity_id,
                self.target_attributes,
                self.current_attributes,
                self.target_statistics_metadata,
                self.state_class,
                self.replay_end_timestamp,
                self.plan,
                self.current_state,
                self.current_state_timestamp,
                self.chunk_size,
            )
            try:
                refresh_recorder_caches(
                    instance,
                    self.target_entity_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Utility Meter reconstruction committed, but Recorder cache refresh failed"
                )

        except Exception as err:  # noqa: BLE001 - recover and propagate to caller
            _LOGGER.exception(
                "Utility Meter reconstruction failed for %s",
                self.target_entity_id,
            )
            try:
                recover_recorder_caches_after_failure(
                    instance,
                    self.target_entity_id,
                )
            except Exception:  # noqa: BLE001 - best-effort recovery after failure
                _LOGGER.exception(
                    "Utility Meter reconstruction failed and Recorder cache recovery also failed"
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
