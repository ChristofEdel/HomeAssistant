"""Davis WeatherLink Local integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WeatherLinkApiClient
from .coordinator import WeatherLinkCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Davis WeatherLink Local from a config entry."""
    api = WeatherLinkApiClient(async_get_clientsession(hass), entry.data[CONF_HOST])
    coordinator = WeatherLinkCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Davis WeatherLink Local config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
