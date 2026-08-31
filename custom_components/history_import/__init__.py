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

DOMAIN: Final = "history_import"
SERVICE_IMPORT: Final = "import"

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

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT,
        handle_import,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True
