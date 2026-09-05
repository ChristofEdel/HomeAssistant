"""Sensor History Import custom integration."""


#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.typing import ConfigType

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Definitions for the actions offered by this integration
#--------------------------------------------------------------------------------

DOMAIN: Final = "history_import"
CHUNK_SIZE: Final = 100

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

from .tasks._helpers import ImportMode
from .tasks.import_history_task import async_import_history
from .csv_reader import TIME_ZONE_LOCAL, TIME_ZONE_UTC
SERVICE_IMPORT_HISTORY: Final = "import"
IMPORT_HISTORY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
        vol.Required("file"): cv.string,
        vol.Required("import_mode"): vol.In(
            [ImportMode.APPEND, ImportMode.OVERWRITE, ImportMode.REPLACE]
        ),
        vol.Optional("time_zone", default=TIME_ZONE_UTC): vol.In(
            [TIME_ZONE_UTC, TIME_ZONE_LOCAL]
        ),
    }
)

from .tasks.copy_task import async_copy_sensor_records
SERVICE_RECALCULATE_STATISTICS: Final = "recalculate"
RECALCULATE_STATISTICS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
    }
)

from .tasks.recalculate_statistics_task import async_recalculate_statistics
SERVICE_COPY: Final = "copy"
COPY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("sensor_from"): cv.entity_id,
        vol.Required("sensor_to"): cv.entity_id,
    }
)

from .tasks.rerun_integration_task import async_rerun_integration_sensor
SERVICE_RERUN_INTEGRATION: Final = "reintegrate"
RERUN_INTEGRATION_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
    }
)

from .tasks.reconstruct_utility_meter_task import async_reconstruct_utility_meter
SERVICE_RECONSTRUCT_UTILITY_METER: Final = "reconstruct_utility_meter"
RECONSTRUCT_UTILITY_METER_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
    }
)

SERVICE_REBUILD: Final = "rebuild"
REBUILD_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
    }
)

#endregion

#--------------------------------------------------------------------------------
#region Consolidated service for rebuilding any sensor type
#--------------------------------------------------------------------------------
async def async_rebuild_sensor(
    hass: HomeAssistant,
    *,
    entity_id: str,
    chunk_size: int,
) -> dict[str, Any]:
    """Rebuild a sensor using the appropriate maintenance operation."""

    sensor_component = hass.data.get(DATA_INSTANCES, {}).get("sensor")
    sensor_entity = (
        sensor_component.get_entity(entity_id)
        if sensor_component is not None
        else None
    )

    if isinstance(sensor_entity, IntegrationSensor):
        return await async_rerun_integration_sensor(
            hass,
            entity_id=entity_id,
            chunk_size=chunk_size,
        )

    if isinstance(sensor_entity, UtilityMeterSensor):
        return await async_reconstruct_utility_meter(
            hass,
            entity_id=entity_id,
            chunk_size=chunk_size,
        )

    return await async_recalculate_statistics(
        hass,
        entity_id=entity_id,
        chunk_size=chunk_size,
    )

#endregion
#--------------------------------------------------------------------------------


#--------------------------------------------------------------------------------
#region Register the services offered by this integration
#--------------------------------------------------------------------------------

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up history_import from services.yaml."""

    async def handle_import(call: ServiceCall) -> dict[str, Any]:
        return await async_import_history(
            hass,
            entity_id=call.data["entity"],
            file_name=call.data["file"],
            mode=ImportMode(call.data["import_mode"]),
            time_zone=call.data["time_zone"],
            chunk_size=CHUNK_SIZE,
        )

    async def handle_copy(call: ServiceCall) -> dict[str, Any]:
        return await async_copy_sensor_records(
            hass,
            source_entity_id=call.data["sensor_from"],
            target_entity_id=call.data["sensor_to"],
            chunk_size=CHUNK_SIZE,
        )

    async def handle_recalculate(call: ServiceCall) -> dict[str, Any]:
        return await async_recalculate_statistics(
            hass,
            entity_id=call.data["entity"],
            chunk_size=CHUNK_SIZE,
        )

    async def handle_reintegrate(call: ServiceCall) -> dict[str, Any]:
        return await async_rerun_integration_sensor(
            hass,
            entity_id=call.data["entity"],
            chunk_size=CHUNK_SIZE,
        )

    async def handle_reconstruct_utility_meter(call: ServiceCall) -> dict[str, Any]:
        return await async_reconstruct_utility_meter(
            hass,
            entity_id=call.data["entity"],
            chunk_size=CHUNK_SIZE,
        )

    async def handle_rebuild(call: ServiceCall) -> dict[str, Any]:
        return await async_rebuild_sensor(
            hass,
            entity_id=call.data["entity"],
            chunk_size=CHUNK_SIZE,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        handle_import,
        schema=IMPORT_HISTORY_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_COPY,
        handle_copy,
        schema=COPY_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALCULATE_STATISTICS,
        handle_recalculate,
        schema=RECALCULATE_STATISTICS_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RERUN_INTEGRATION,
        handle_reintegrate,
        schema=RERUN_INTEGRATION_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECONSTRUCT_UTILITY_METER,
        handle_reconstruct_utility_meter,
        schema=RECONSTRUCT_UTILITY_METER_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD,
        handle_rebuild,
        schema=REBUILD_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True

#endregion
#--------------------------------------------------------------------------------


