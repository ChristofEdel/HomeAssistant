"""Action for selecting recorder history to push to InfluxDB."""

from datetime import datetime, timedelta
from fnmatch import fnmatchcase
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import is_entity_recorded
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PUSH_TO_INFLUXDB_SCHEMA = vol.Schema({
    vol.Optional("entities"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("entity_globs"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("from"): cv.datetime,
    vol.Optional("back_until"): cv.datetime,
    vol.Optional("from_days"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("back_until_days"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Required("chunk_size"): vol.All(vol.Coerce(int), vol.Range(min=1)),
})


async def async_push_to_influxdb(call: ServiceCall) -> None:
    """Validate the action input and select entities to push."""
    data = call.data
    hass = call.hass
    config: dict[str, Any] | None = hass.data.get(DOMAIN)
    if config is None:
        raise ServiceValidationError("Simple InfluxDB has no loaded config entry")

    if not data.get("entities") and not data.get("entity_globs"):
        raise ServiceValidationError("Specify entities or entity_globs")

    has_absolute = "from" in data or "back_until" in data
    has_days = "from_days" in data or "back_until_days" in data
    if has_absolute == has_days:
        raise ServiceValidationError("Specify either from/back_until or from_days/back_until_days")
    if has_absolute:
        if "from" not in data or "back_until" not in data:
            raise ServiceValidationError("Both from and back_until are required")
        from_time = dt_util.as_utc(data["from"])
        back_until = dt_util.as_utc(data["back_until"])
    else:
        if "from_days" not in data or "back_until_days" not in data:
            raise ServiceValidationError("Both from_days and back_until_days are required")
        midnight = dt_util.start_of_local_day()
        from_time = dt_util.as_utc(midnight + timedelta(days=1 - data["from_days"]))
        back_until = dt_util.as_utc(midnight - timedelta(days=data["back_until_days"]))

    if back_until >= from_time:
        raise ServiceValidationError("back_until must be before from")

    selected = set(data.get("entities", []))
    globs = data.get("entity_globs", [])
    if globs and DATA_INSTANCE in hass.data:
        entity_filter = convert_include_exclude_filter(config)
        known_entities = set(hass.states.async_entity_ids()) | set(er.async_get(hass).entities)
        for entity_id in known_entities:
            if (
                entity_id not in selected
                and any(fnmatchcase(entity_id, glob) for glob in globs)
                and entity_filter(entity_id)
                and is_entity_recorded(hass, entity_id)
            ):
                selected.add(entity_id)

    for entity_id in sorted(selected):
        await push_entity_to_influxdb(entity_id, from_time, back_until, data["chunk_size"])


async def push_entity_to_influxdb(
    entity_id: str, from_time: datetime, back_until: datetime, chunk_size: int
) -> None:
    """Log the intended history push until the actual copy is implemented."""
    _LOGGER.info(
        "push_entity_to_influxdb(entity_id=%s, from=%s, back_until=%s, chunk_size=%s)",
        entity_id, from_time, back_until, chunk_size,
    )
