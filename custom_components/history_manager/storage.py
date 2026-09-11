#--------------------------------------------------------------------------------------------------
#
# history_manager/storage.py
#
#  - async_get_runtime_storage     -> RuntimeStorage      Obtains the component's runtime 
#                                                         storage data (cleared on restart)
#
#--------------------------------------------------------------------------------------------------
# Code based on the original  "GUI Recorder" by ideaalab, https://github.com/ideaalab/gui-recorder

#--------------------------------------------------------------------------------------------------
# region Imports and constants
#--------------------------------------------------------------------------------------------------

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.data"

# endregion
#--------------------------------------------------------------------------------------------------

#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

@dataclass
class RuntimeStorage:
    config: dict = field(default_factory=dict)
    websocket_registered: bool = False
    custom_card_registered: bool = False
    static_path_registered: bool = False

#region Imports
#--------------------------------------------------------------------------------------------------

def get_runtime_storage(
    hass: HomeAssistant,
) -> RuntimeStorage:
    """Return the History Manager runtime storage."""

    domain_data = hass.data.setdefault(DOMAIN, {})

    storage = domain_data.get("runtime_storage")
    if storage is None:
        storage = RuntimeStorage()
        domain_data["runtime_storage"] = storage

    return storage

async def async_get_runtime_storage(
    hass: HomeAssistant,
) -> RuntimeStorage:
    return get_runtime_storage(hass)


