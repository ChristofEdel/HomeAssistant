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
from .maintenance_task import (
    async_copy_sensor_records,
    async_recalculate_statistics,
    async_reintegrate_sensor,
)

DOMAIN: Final = "history_import"
SERVICE_IMPORT: Final = "import"
SERVICE_RECALCULATE: Final = "recalculate"
SERVICE_COPY: Final = "copy"
SERVICE_REINTEGRATE: Final = "reintegrate"

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
        vol.Optional("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

RECALCULATE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
        vol.Optional("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

COPY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("sensor_from"): cv.entity_id,
        vol.Required("sensor_to"): cv.entity_id,
        vol.Optional("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

REINTEGRATE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
        vol.Optional("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
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
            chunk_size=call.data.get("chunk_size"),
        )

    async def handle_recalculate(call: ServiceCall) -> dict[str, Any]:
        return await async_recalculate_statistics(
            hass,
            entity_id=call.data["entity"],
            chunk_size=call.data.get("chunk_size"),
        )

    async def handle_copy(call: ServiceCall) -> dict[str, Any]:
        return await async_copy_sensor_records(
            hass,
            source_entity_id=call.data["sensor_from"],
            target_entity_id=call.data["sensor_to"],
            chunk_size=call.data.get("chunk_size"),
        )

    async def handle_reintegrate(call: ServiceCall) -> dict[str, Any]:
        return await async_reintegrate_sensor(
            hass,
            entity_id=call.data["entity"],
            chunk_size=call.data.get("chunk_size"),
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

    hass.services.async_register(
        DOMAIN,
        SERVICE_REINTEGRATE,
        handle_reintegrate,
        schema=REINTEGRATE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True
