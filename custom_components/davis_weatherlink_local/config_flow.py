"""Config flow for Davis WeatherLink Local."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    WeatherLinkApiClient,
    WeatherLinkApiError,
    normalize_base_url,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the setup/reconfiguration schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST,
                default=defaults.get(CONF_HOST, ""),
            ): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
        }
    )


async def _async_validate_input(hass, host: str) -> tuple[str, str]:
    """Validate connectivity and return normalized URL plus device ID."""
    base_url = normalize_base_url(host)
    client = WeatherLinkApiClient(async_get_clientsession(hass), base_url)
    payload = await client.async_get_current_conditions()
    return base_url, str(payload["data"]["did"])


class DavisWeatherLinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Davis WeatherLink Local."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url, did = await _async_validate_input(
                    self.hass,
                    user_input[CONF_HOST],
                )
            except WeatherLinkApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(did)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Davis WeatherLink {did}",
                    data={
                        CONF_HOST: base_url,
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow the host or polling interval to be changed."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url, did = await _async_validate_input(
                    self.hass,
                    user_input[CONF_HOST],
                )
            except WeatherLinkApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(did)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"Davis WeatherLink {did}",
                    data={
                        CONF_HOST: base_url,
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or dict(entry.data)),
            errors=errors,
        )
