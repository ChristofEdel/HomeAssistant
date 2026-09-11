#--------------------------------------------------------------------------------------------------
#
# history_manager/__init__.py
#
#--------------------------------------------------------------------------------------------------
# Code based on the original  "GUI Recorder" by ideaalab, https://github.com/ideaalab/gui-recorder

#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

from __future__ import annotations

import logging

from pathlib import Path
from datetime import datetime, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import SERVICE_RELOAD
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection

from .const import CARD_STYLE_URL, CARD_MODULE_URL, DOMAIN
from .db_stats import is_sqlite_recorder
from .websocket_api import async_setup_websocket_api
from .yaml_config import (
    CONFIG_SCHEMA, # noqa: F401      unused, but MUST be imported
    async_integration_yaml_config, 
    async_apply_yaml_config, 
    async_reload_yaml
)
from .storage import async_get_runtime_storage
from .short_retention_entities import async_purge_short_entities
from .unrecorded_entities import async_purge_unrecorded_entities

_LOGGER = logging.getLogger(__name__)

# endregion
#--------------------------------------------------------------------------------------------------


async def async_setup(
    hass: HomeAssistant,
    _config: ConfigType,
) -> bool:
    """Set up the History Manager custom component."""

    # Register the reload service to be called when Home Assistant's 
    # "Quick reload" / "Reload YAML" operation is invoked.

    async def reload_yaml(_call: ServiceCall) -> None:
        await async_reload_yaml(hass)
        
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RELOAD,
        reload_yaml
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    # Ensure this component only is set up once.
    if is_sqlite_recorder(hass) is False:
        _LOGGER.error(
            "History Manager only supports the SQLite recorder backend. "
            "Detected a different db_url; aborting setup."
        )
        return False

    # Get the YAML configuration passed by Home Assistant.
    config = await async_integration_yaml_config(hass, DOMAIN, raise_on_failure=True)
    await async_apply_yaml_config(hass, config)

    # Register the websocket if required
    runtime_storage = await async_get_runtime_storage(hass)
    if not runtime_storage.websocket_registered:
        await async_setup_websocket_api(hass)
        runtime_storage.websocket_registered = True

    if not runtime_storage.custom_card_registered:
        try:
            await _async_register_custom_card(hass)
            runtime_storage.custom_card_registered = True
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to register history-manager-card")

    # Register the actions 'purge_short_entities' and 'purge_unrecorded_entities'
    async def _handle_purge_short_entities(call: ServiceCall) -> None:
        await async_purge_short_entities(hass)

    hass.services.async_register(DOMAIN, "purge_short_entities", _handle_purge_short_entities)

    async def _handle_purge_unrecorded_entities(call: ServiceCall) -> None:
        await async_purge_unrecorded_entities(hass)

    hass.services.async_register(DOMAIN, "purge_unrecorded_entities", _handle_purge_unrecorded_entities)


    # Set up a daily task to call the purge actions
    purge_time = time.fromisoformat(entry.data["purge_time"])

    async def _run_daily_purge(_now: datetime) -> None:
        _LOGGER.info("Running daily purge of short-recording and unrecorded entities")
        await hass.services.async_call(DOMAIN, "purge_short_entities", blocking=True)
        await hass.services.async_call(DOMAIN, "purge_unrecorded_entities", blocking=True)

    cancel_timer_function = async_track_time_change(
            hass,
            _run_daily_purge,
            hour=purge_time.hour,
            minute=purge_time.minute,
            second=purge_time.second,
        )

    entry.async_on_unload(cancel_timer_function)

    _LOGGER.debug("History Manager initialized")
    return True


async def async_unload_entry(hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "purge_short_entities")
    hass.services.async_remove(DOMAIN, "purge_unrecorded_entities")
    await _async_deregister_custom_card(hass)
    return True

async def _async_register_custom_card(hass: HomeAssistant) -> None:

    # get the location of the JS and CSS files included in component package
    frontend_path = Path(__file__).parent / "frontend"

    # register the static path (unless already done)
    runtime_storage = await async_get_runtime_storage(hass)
    if not runtime_storage.static_path_registered:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    CARD_MODULE_URL,
                    str(frontend_path / "history-manager-card.js"),
                    cache_headers=False,
                ),
                StaticPathConfig(
                    CARD_STYLE_URL,
                    str(frontend_path / "history-manager-card.css"),
                    cache_headers=False,
                ),
            ]
        )
        runtime_storage.static_path_registered = True

    # register the custom card as lovelace resource
    # (only works if lovelace resources are maintained in storage mode)
    lovelace_data = hass.data.get(LOVELACE_DATA)
    resources = lovelace_data.resources if lovelace_data is not None else None
    if isinstance(resources, ResourceStorageCollection):
        await resources.async_get_info()

        for resource in resources.async_items():
            if resource["url"].split("?")[0] == CARD_MODULE_URL:
                return

        await resources.async_create_item(
            {
                "url": CARD_MODULE_URL,
                "res_type": "module",
            }
        )
        _LOGGER.debug("Custom card registered")

    # Fallback: register as extra js url.
    # NB - this leads to configuration error after initial HASS load in browser
    # https://github.com/home-assistant/frontend/issues/52570
    else:
        _LOGGER.warning(
            "Lovelace resources are not using yaml mode, not storage mode - cannot"
            "register the custom card. Using add_extra_js_url() instead, but due to"
            "a Home Assistant frontend race condition, hard browser reloads may"
            "temporarily show 'Configuration error'."
        )
        add_extra_js_url(hass, CARD_MODULE_URL)



async def _async_deregister_custom_card(hass: HomeAssistant) -> None:

    lovelace_data = hass.data.get(LOVELACE_DATA)
    resources = lovelace_data.resources if lovelace_data is not None else None

    if isinstance(resources, ResourceStorageCollection):
        await resources.async_get_info()
        for resource in resources.async_items():
            if resource["url"].split("?")[0] == CARD_MODULE_URL:
                await resources.async_delete_item(resource["id"])
                return
    else:
        remove_extra_js_url(hass, CARD_MODULE_URL)
        return