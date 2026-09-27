"""Support for sending data to an Influx database."""

import copy
from collections.abc import Callable
from typing import Any

from homeassistant.components.recorder.entity_options import is_entity_recorded
from homeassistant.const import CONF_UNIT_OF_MEASUREMENT
from homeassistant.core import Event, State, HomeAssistant
from homeassistant.helpers.entityfilter import (convert_include_exclude_filter)
from homeassistant.helpers.recorder import DATA_INSTANCE

from .const import CONF_EXCLUDE_UNRECORDED

PROPERTY_ATTRIBUTES_BY_DOMAIN = {
    "climate": {
        "current_temperature":  "temperature",
        "temperature":          "temperature",
        "target_temp_high":     "temperature",
        "target_temp_low":      "temperature",
        "current_humidity":     "%",
        "humidity":             "%",
        "hvac_action":          None,
        "fan_mode":             None,
        "preset_mode":          None,
        "swing_mode":           None,
        "swing_horizontal_mode":None,
    },
    "cover": {
        "current_position":     "%",
        "current_tilt_position":"%",
    },
    "fan": {
        "percentage":  "%",
        "oscillating": None,
        "direction":   None,
        "preset_mode": None,
    },
}

def get_event_to_json(hass: HomeAssistant, conf: dict) -> Callable[[Event], list[dict[str, Any]] | None]:
    """Build event to json converter and add to config."""


    entity_filter = convert_include_exclude_filter(conf)

    def event_to_json(event: Event) -> list[dict[str, Any]] | None:
        """Convert event into json in format Influx expects."""

        result: list[dict[str, Any]] = []

        # get the event state, and skip filtered entities
        entity_id = event.data["entity_id"]
        if not entity_filter(entity_id):
            return None
        if conf.get(CONF_EXCLUDE_UNRECORDED, True) and (
            DATA_INSTANCE not in hass.data or not is_entity_recorded(hass, entity_id)
        ):
            return None
        domain, object_id = entity_id.split(".", 1)
        state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")

        # get the unit of measurement. If entity is deleted (state is none),
        # try to get from the previous state
        if state is not None:
            entity_uom      = state.attributes.get(CONF_UNIT_OF_MEASUREMENT)
        elif old_state is not None:
            entity_uom      = old_state.attributes.get(CONF_UNIT_OF_MEASUREMENT)
        else:
            entity_uom = None

        temperature_uom = hass.config.units.temperature_unit


        # Assemble the JSON common for all entries
        base_json: dict[str, Any] = {
            "time": event.time_fired,
            "measurement": domain,          # table / "measurement"
            "tags": {
                "entity_id": object_id,
            },
            "fields": {},
        }

        # create and fill the JSON for the entity main state
        if (
            old_state is None                   # first reported --> send
            or state is None                    # entity deleted --> send None
            or state.state != old_state.state   # changed --> send
        ):
            state_json = copy.deepcopy(base_json)
            state_json["tags"]["property"] = "state"
            if entity_uom is not None:
                state_json["fields"]["unit"] = entity_uom
            _add_value_to_json(state_json, state.state if state is not None else None)

            result.append(state_json)

        attributes_to_include = PROPERTY_ATTRIBUTES_BY_DOMAIN.get(domain, {})

        # then, handle all attributes configured for this domain
        # (if any)
        for attribute_key, uom in attributes_to_include.items():

            attribute_value = (
                state.attributes.get(attribute_key)
                if state is not None
                else None
            )
            old_attribute_value = (
                old_state.attributes.get(attribute_key)
                if old_state is not None
                else None
            )

            # skip state changes where the value does not change
            if (
                old_state is not None  # must NOT be first state
                and state is not None  # must NOT be entity deletion
                and attribute_value == old_attribute_value
            ):
                continue

            attribute_json = copy.deepcopy(base_json)
            attribute_json["tags"]["property"] = attribute_key

            if uom == "entity":
                if entity_uom is not None: 
                    attribute_json["fields"]["unit"] = entity_uom
            elif uom == "temperature":
                if temperature_uom is not None: 
                    attribute_json["fields"]["unit"] = temperature_uom
            elif uom is not None:
                attribute_json["fields"]["unit"] = uom

            _add_value_to_json(attribute_json, attribute_value)
                
            result.append(attribute_json)

        return result

    return event_to_json

def _add_value_to_json(json: dict[str, Any], value: Any) -> None:
    """Add a value to an InfluxDB JSON point."""

    if value is None:
        json["fields"]["value_str"] = 'None'
        return

    if isinstance(value, bool):
        json["fields"]["value_str"] = str(value).lower()
        return

    try:
        json["fields"]["value"] = float(value)
    except (ValueError, TypeError):
        json["fields"]["value_str"] = str(value)
