
from typing import Any, cast
import voluptuous as vol


from homeassistant import config as conf_util
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entityfilter import INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA
from .const import (
    DOMAIN,
    CONF_MAX_RETRIES,
    CONF_INCLUDE,
    CONF_EXCLUDE,
)
from .influx_thread import InfluxThread
from .influx_connection import get_influx_connection
from .event_to_json import get_event_to_json

#--------------------------------------------------------------------------------------------------
# region Configuration Schema
#--------------------------------------------------------------------------------------------------

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA.extend( {
            vol.Optional(CONF_MAX_RETRIES, default=0): cv.positive_int,
        })
    },
    extra=vol.ALLOW_EXTRA,
)

type InfluxDBConfigEntry = ConfigEntry[InfluxThread]

# endregion
#--------------------------------------------------------------------------------------------------

async def async_setup(_hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the InfluxDB component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InfluxDBConfigEntry) -> bool:
    """Set up InfluxDB from a config entry."""


    # Create a config dictionary from the config entry and 
    # YAML for the domain
    data = entry.data
    hass_config = await conf_util.async_hass_config_yaml(hass)

    influx_yaml = cast(dict[str, Any], CONFIG_SCHEMA(hass_config)).get(DOMAIN, {})
    default_filter_settings: dict[str, Any] = {
        "entity_globs": [],
        "entities": [],
        "domains": [],
    }

    config = data | {
        CONF_MAX_RETRIES: influx_yaml.get(CONF_MAX_RETRIES, 0),
        CONF_INCLUDE: influx_yaml.get(CONF_INCLUDE, default_filter_settings),
        CONF_EXCLUDE: influx_yaml.get(CONF_EXCLUDE, default_filter_settings),
    }

    # Try to connect to the InfluxDB database.
    try:
        influx = await hass.async_add_executor_job(get_influx_connection, config, True)
    except ConnectionError as err:
        raise ConfigEntryNotReady(err) from err

    # start the main processing thread
    influx_thread = InfluxThread(
        hass, entry, influx, 
        get_event_to_json(hass, config),    # the function that translates events to JSON for inluxDB
        config[CONF_MAX_RETRIES]
    )
    await hass.async_add_executor_job(influx_thread.start)

    entry.runtime_data = influx_thread

    return True


async def async_unload_entry(hass: HomeAssistant, entry: InfluxDBConfigEntry) -> bool:
    """Unload a config entry."""
    influx_thread = entry.runtime_data

    # Run shutdown in the executor so the event loop isn't blocked
    await hass.async_add_executor_job(influx_thread.shutdown)

    return True

