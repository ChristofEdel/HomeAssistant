"""Config flow for InfluxDB integration."""

import logging
from pathlib import Path
import shutil
from typing import Any, override

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.storage import STORAGE_DIR

from .const import (
    DOMAIN, 
    CONF_SSL_CA_CERT,
    CONF_BUCKET
)
from .influx_connection import get_influx_connection


_LOGGER = logging.getLogger(__name__)

INFLUXDB_V2_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default="https://"): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.URL,
                autocomplete="url",
            ),
        ),
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
        vol.Required(CONF_BUCKET): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.TEXT,
            ),
        ),
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD,
            ),
        ),
        vol.Optional(CONF_SSL_CA_CERT): FileSelector(
            FileSelectorConfig(accept=".pem,.crt,.cer,.der")
        ),
    }
)


async def _validate_influxdb_connection(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, str]:
    """Validate connection to influxdb."""

    def _test_connection() -> None:
        influx = get_influx_connection(data, test_write=True)
        influx.close()

    errors = {}

    try:
        await hass.async_add_executor_job(_test_connection)
    except ConnectionError as ex:
        _LOGGER.error(ex)
        if "SSLError" in ex.args[0]:
            errors = {"base": "ssl_error"}
        elif "database not found" in ex.args[0]:
            errors = {"base": "invalid_database"}
        elif "authorization failed" in ex.args[0]:
            errors = {"base": "invalid_auth"}
        elif "token" in ex.args[0]:
            errors = {"base": "invalid_config"}
        else:
            errors = {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unknown error")
        errors = {"base": "unknown"}

    return errors


async def _save_uploaded_cert_file(hass: HomeAssistant, uploaded_file_id: str) -> Path:
    """Move the uploaded file to storage directory."""

    def _process_upload() -> Path:
        with process_uploaded_file(hass, uploaded_file_id) as file_path:
            dest_path = Path(hass.config.path(STORAGE_DIR, DOMAIN))
            dest_path.mkdir(exist_ok=True)
            file_name = f"influxdb{file_path.suffix}"
            dest_file = dest_path / file_name
            shutil.move(file_path, dest_file)
        return dest_file

    return await hass.async_add_executor_job(_process_upload)


class SimpleInfluxDBConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for InfluxDB."""

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step when user configures InfluxDB v2."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_URL: user_input[CONF_URL],
                CONF_TOKEN: user_input[CONF_TOKEN],
                CONF_BUCKET: user_input[CONF_BUCKET],
                CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
            }
            if (cert := user_input.get(CONF_SSL_CA_CERT)) is not None:
                path = await _save_uploaded_cert_file(self.hass, cert)
                data[CONF_SSL_CA_CERT] = str(path)
            errors = await _validate_influxdb_connection(self.hass, data)

            if not errors:
                title = f"{data[CONF_BUCKET]} ({data[CONF_URL]})"
                return self.async_create_entry(title=title, data=data)

        schema = INFLUXDB_V2_SCHEMA

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of InfluxDB v2."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            data = {
                CONF_URL: user_input[CONF_URL],
                CONF_TOKEN: user_input[CONF_TOKEN],
                CONF_BUCKET: user_input[CONF_BUCKET],
                CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
            }
            if (cert := user_input.get(CONF_SSL_CA_CERT)) is not None:
                path = await _save_uploaded_cert_file(self.hass, cert)
                data[CONF_SSL_CA_CERT] = str(path)
            elif CONF_SSL_CA_CERT in entry.data:
                data[CONF_SSL_CA_CERT] = entry.data[CONF_SSL_CA_CERT]
            errors = await _validate_influxdb_connection(self.hass, data)

            if not errors:
                title = f"{data[CONF_BUCKET]} ({data[CONF_URL]})"
                return self.async_update_reload_and_abort(
                    entry, title=title, data_updates=data
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                INFLUXDB_V2_SCHEMA, entry.data | (user_input or {})
            ),
            errors=errors,
        )

