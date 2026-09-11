#--------------------------------------------------------------------------------------------------
#
# history_manager/websocket_api.py
#
#--------------------------------------------------------------------------------------------------
# Code based on the original  "GUI Recorder" by ideaalab, https://github.com/ideaalab/gui-recorder

#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

import voluptuous as vol
import yaml
from typing import Any

from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.decorators import websocket_command, async_response, require_admin
from homeassistant.core import HomeAssistant, callback

from .db_stats import async_get_db_size, async_get_table_data
from .entity_tree import build_entity_tree
from .storage import async_get_runtime_storage
from .short_retention_entities import get_all_short_retention_entity_ids
from .unrecorded_entities import get_all_unrecorded_entity_ids

# endregion
#--------------------------------------------------------------------------------------------------

async def async_setup_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_analyze_db)
    websocket_api.async_register_command(hass, ws_get_table_data)
    websocket_api.async_register_command(hass, ws_get_entity_tree)
    websocket_api.async_register_command(hass, ws_get_config)

@websocket_command({vol.Required("type"): "history_manager/analyze_db"})
@require_admin
@async_response
async def ws_analyze_db(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    stats = await async_get_db_size(hass)
    connection.send_result(msg["id"], {"ok": True, "stats": stats})

@websocket_command({vol.Required("type"): "history_manager/get_table_data"})
@require_admin
@async_response
async def ws_get_table_data(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    table_data = await async_get_table_data(hass)
    connection.send_result(msg["id"], {"ok": True, "table_data": table_data})


@websocket_command({vol.Required("type"): "history_manager/get_entity_tree"})
@require_admin
@async_response
async def ws_get_entity_tree(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    result = await hass.async_add_executor_job(
        build_entity_tree,
        hass,
    )
    connection.send_result(
        msg["id"],
        result,
    )
@websocket_command({vol.Required("type"): "history_manager/get_config"})
@require_admin
@async_response
async def ws_get_config(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    result = await async_get_runtime_storage(hass) or {}
    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "config": result.config,
            "config_yaml": yaml.safe_dump(
                _yaml_safe(result.config),
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ),
            "short_entities": get_all_short_retention_entity_ids(hass),
            "unrecorded_entities": get_all_unrecorded_entity_ids(hass),
        },
    )

def _yaml_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _yaml_safe(val) for key, val in value.items()}
    if isinstance(value, set):
        return [_yaml_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_yaml_safe(item) for item in value]
    return value
