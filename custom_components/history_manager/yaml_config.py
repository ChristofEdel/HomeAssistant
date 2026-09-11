#--------------------------------------------------------------------------------------------------
#
# history_manager/yaml_config.py
#
# short_retention:
#   purge_keep_days:  30                 The number of entities to keep
#   entities:                            A list of entities to which to apply this retention
#     - ...                              
#   entity_globs:                        Globs to select entities to which to apply this
#     - ...
#   except:                              A list of entities to which NOT apply this retention
#     - ...
#   except_globs:                        Globs to exclude entities
#     - ...
#
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

import voluptuous as vol
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .storage import async_get_runtime_storage

DEFAULT_RETENTION: Final[int] = 14

# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Configuration schema definition
#--------------------------------------------------------------------------------------------------

_SHORT_RETENTION_SCHEMA = vol.Schema(
    {
        vol.Optional("purge_keep_days"): vol.All(
            vol.Coerce(int),
            vol.Range(min=1),
        ),
        vol.Optional("entities"): vol.All(
            cv.ensure_list,
            [cv.entity_id],
        ),
        vol.Optional("entity_globs"): vol.All(
            cv.ensure_list,
            [cv.string],
        ),
        vol.Optional("except"): vol.All(
            cv.ensure_list,
            [cv.entity_id],
        ),
        vol.Optional("except_globs"): vol.All(
            cv.ensure_list,
            [cv.string],
        ),
    }
)

_HISTORY_MANAGER_SCHEMA = vol.Schema(
    {
        vol.Optional("short_retention"): _SHORT_RETENTION_SCHEMA,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.All(
            cv.ensure_list,
            [_HISTORY_MANAGER_SCHEMA],
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Loading / unloading
#--------------------------------------------------------------------------------------------------

async def async_apply_yaml_config(
    hass: HomeAssistant,
    config: ConfigType,
) -> None:
    """Consolidate YAML configuration and store it."""

    yaml_data = consolidate_yaml_config(
        config.get(DOMAIN, [])
    )

    runtime_storage = await async_get_runtime_storage(hass)
    runtime_storage.config = yaml_data

async def async_reload_yaml(
    hass: HomeAssistant
) -> None:
    """Reload History Manager YAML configuration."""

    config = await async_integration_yaml_config(
        hass,
        DOMAIN,
        raise_on_failure=True,
    )

    await async_apply_yaml_config(hass, config)

# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region consolidate_yaml_config
#--------------------------------------------------------------------------------------------------

def consolidate_yaml_config(configs: list[dict]) -> dict:

    # we start with the default retention period configured
    short_retention = {
        "purge_keep_days": DEFAULT_RETENTION,
        "entities": [],
        "entity_globs": [],
        "except": [],
        "except_globs": [],
    }

    # Iterate over all configurations and merge them together.
    # lists get merged, and purge_keep_days takes the latest value encountered
    for config in configs:
        short = config.get("short_retention")
        if not short:
            continue

        if "purge_keep_days" in short:
            short_retention["purge_keep_days"] = short["purge_keep_days"]

        short_retention["entities"].extend(
            short.get("entities", [])
        )
        short_retention["entity_globs"].extend(
            short.get("entity_globs", [])
        )
        short_retention["except"].extend(
            short.get("except", [])
        )
        short_retention["except_globs"].extend(
            short.get("except_globs", [])
        )


    short_retention["entities"]     = set(_cleanup_string_list(short_retention["entities"]))
    short_retention["entity_globs"] = _cleanup_string_list(short_retention["entity_globs"])
    short_retention["except"]       = set(_cleanup_string_list(short_retention["except"]))
    short_retention["except_globs"] = _cleanup_string_list(short_retention["except_globs"])

    return { 
        "short_retention": short_retention,
    }

def _cleanup_string_list(values) -> list[str]:
    """Trim, sort and deduplicate a list of strings"""
    normalized: list[str] = []
    seen: set[str] = set()

    for value in values or []:
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)

    return sorted(normalized)

# endregion
#--------------------------------------------------------------------------------------------------
