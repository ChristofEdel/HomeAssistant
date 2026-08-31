"""Sensor History Import custom integration."""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .csv_reader import TIME_ZONE_LOCAL, TIME_ZONE_UTC
from .import_task import async_import_history
from .importer import ImportMode
from .maintenance_task import async_copy_sensor_records, async_recalculate_statistics

DOMAIN: Final = "history_import"
SERVICE_IMPORT: Final = "import"
SERVICE_RECALCULATE: Final = "recalculate"
SERVICE_COPY: Final = "copy"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
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

RECALCULATE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
    }
)

COPY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("sensor_from"): cv.entity_id,
        vol.Required("sensor_to"): cv.entity_id,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up history_import from services.yaml."""

    async def handle_import(call: ServiceCall) -> dict[str, Any]:
        return await async_import_history(
            hass,
            entity_id=call.data["entity"],
            file_name=call.data["file"],
            mode=ImportMode(call.data["import_mode"]),
            time_zone=call.data["time_zone"],
        )

    async def handle_recalculate(call: ServiceCall) -> dict[str, Any]:
        return await async_recalculate_statistics(
            hass,
            entity_id=call.data["entity"],
        )

    async def handle_copy(call: ServiceCall) -> dict[str, Any]:
        return await async_copy_sensor_records(
            hass,
            source_entity_id=call.data["sensor_from"],
            target_entity_id=call.data["sensor_to"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT,
        handle_import,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALCULATE,
        handle_recalculate,
        schema=RECALCULATE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_COPY,
        handle_copy,
        schema=COPY_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True
