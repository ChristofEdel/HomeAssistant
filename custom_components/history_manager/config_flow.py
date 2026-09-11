#--------------------------------------------------------------------------------------------------
#
# history_manager/config_flow.py
#
#--------------------------------------------------------------------------------------------------
# Code based on the original  "GUI Recorder" by ideaalab, https://github.com/ideaalab/gui-recorder


#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

from __future__ import annotations
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TimeSelector

from .const import DOMAIN
from .db_stats import is_sqlite_recorder

# endregion
#--------------------------------------------------------------------------------------------------

class HistoryManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        sqlite_check = is_sqlite_recorder(self.hass)
        if sqlite_check is False:
            return self.async_abort(reason="unsupported_database")
        if sqlite_check is None:
            return self.async_abort(reason="recorder_not_ready")

        return self.async_create_entry(title="History Manager", data={})


    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        sqlite_check = is_sqlite_recorder(self.hass)
        if sqlite_check is False:
            return self.async_abort(reason="unsupported_database")
        if sqlite_check is None:
            return self.async_abort(reason="recorder_not_ready")

        if user_input is not None:
            return self.async_create_entry(
                title="History Manager",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "purge_time",
                        default="02:00:00",
                    ): TimeSelector(),
                }
            ),
        )