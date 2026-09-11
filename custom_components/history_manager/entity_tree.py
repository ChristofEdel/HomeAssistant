#--------------------------------------------------------------------------------------------------
#
# history_manager/entity_tree.py
#
#  - build_entity_tree -> dict             Build a tree of all devices with their associated 
#                                          entities, as well as lists with standalone entities
#                                          and obsolete_entities
#
#--------------------------------------------------------------------------------------------------

#--------------------------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------------------------

from . import tracing

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance as get_recorder_instance

from .const import INTEGRATION_VERSION
from .db_stats import connect_to_db
from .storage import get_runtime_storage
from .short_retention_entities import is_short_retention_entity

#endregion
#--------------------------------------------------------------------------------------------------

@callback
def build_entity_tree(hass: HomeAssistant) -> dict:

    f = tracing.enter("build_entity_tree")

    try:
        # Collect the information that we need in this function
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        recorder = get_recorder_instance(hass)
        entity_filter = recorder.entity_filter


        runtime_storage = get_runtime_storage(hass)
        config = runtime_storage.config
        config_short_retention = config["short_retention"]
        entity_counts, total_record_count = _get_entity_counts(hass)

        devices: dict[str, dict] = {}
        standalone_entities: list[dict] = []
        obsolete_entities: list[dict] = []
        processed_entity_ids: set[str] = set()
        device_entity_count: int = 0

        def row_sort_key(entity: dict) -> tuple:
            return (-int(entity.get("record_count", 0)), entity["entity_id"])

        # go through all entities in the entity registry and
        #  - add them to "processed_entity_ids"
        #  - create a row with the data for each entity
        #  - if the entity has a device_id, add it to the corresponding device in "devices"
        #  - if it has NO device_id, add it to "standalone_entities"
        for entity_entry in entity_registry.entities.values():
            processed_entity_ids.add(entity_entry.entity_id)
            recorded = entity_filter is None or entity_filter(entity_entry.entity_id)
            row = {
                "entity_id":      entity_entry.entity_id,
                "device_id":      entity_entry.device_id,
                "name":           entity_entry.original_name or entity_entry.name or entity_entry.entity_id,
                "platform":       entity_entry.platform,
                "domain":         entity_entry.domain,
                "recorder_off":   not recorded,
                "recorder_short": recorded and is_short_retention_entity(config_short_retention, entity_entry.entity_id),
                "disabled_by":    str(entity_entry.disabled_by) if entity_entry.disabled_by else None,
                "hidden_by":      str(entity_entry.hidden_by) if entity_entry.hidden_by else None,
                "record_count":   int(entity_counts.get(entity_entry.entity_id, 0)),
            }
            row["recorder_standard"] = recorded and not row["recorder_short"]

            if entity_entry.device_id:
                device = device_registry.async_get(entity_entry.device_id)
                if device:
                    dev = devices.setdefault(
                        device.id,
                        {
                            "device_id": device.id,
                            "name": device.name_by_user or device.name or "Unnamed device",
                            "manufacturer": device.manufacturer,
                            "model": device.model,
                            "entities": [],
                            "record_count": 0,
                            "recorder_standard_count": 0,
                            "recorder_short_count": 0,
                            "recorder_off_count": 0,
                            "recorder_standard_record_count": 0,
                            "recorder_short_record_count": 0,
                            "recorder_off_record_count": 0,
                        },
                    )
                    record_count = row["record_count"]
                    device_entity_count += 1
                    dev["entities"].append(row)
                    dev["record_count"] += record_count
                    if row["recorder_standard"]:
                        dev["recorder_standard_count"] += 1
                        dev["recorder_standard_record_count"] += record_count
                    if row["recorder_short"]:
                        dev["recorder_short_count"] += 1
                        dev["recorder_short_record_count"] += record_count
                    if row["recorder_off"]:
                        dev["recorder_off_count"] += 1
                        dev["recorder_off_record_count"] += record_count
            else:                    
                standalone_entities.append(row)

        # mix the entities that are in the database but not in the current entity 
        # registry (i.e. obsolete entities, or some legacy entities that are not 
        # in the entity registry)
        live_entity_ids = set(hass.states.async_entity_ids())

        for entity_id, count in entity_counts.items():
            if entity_id in processed_entity_ids:
                continue # already handled elsewhere
            processed_entity_ids.add(entity_id)

            domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
            recorded = entity_filter is None or entity_filter(entity_id)
            row = {
                "entity_id":      entity_id,
                "device_id":      None,
                "name":           "-",
                "platform":       domain,
                "domain":         domain,
                "recorder_off":   not recorded,
                "recorder_short": recorded and is_short_retention_entity(config_short_retention, entity_id),
                "disabled_by":    None,
                "hidden_by":      None,
                "record_count":   int(count),
            }
            row["recorder_standard"] = recorded and not row["recorder_short"]

            if entity_id in live_entity_ids:
                # Has a live state but no entity_registry entry (e.g. legacy YAML
                # platforms like snmp/template/command_line without unique_id).
                # It's not obsolete, just unregistered - list it as an standalone.
                state_obj = hass.states.get(entity_id)
                row["name"] = state_obj.attributes.get("friendly_name") if state_obj else entity_id
                standalone_entities.append(row)
            else:
                row["obsolete"] = True
                obsolete_entities.append(row)

        # Sort the devices and entities for display
        device_rows = sorted(devices.values(), key=lambda d: (-int(d.get("record_count", 0)), d["name"].lower()))
        for dev in device_rows:
            dev["entities"] = sorted(dev["entities"], key=row_sort_key)
        standalone_entities = sorted(standalone_entities, key=row_sort_key)
        obsolete_entities = sorted(obsolete_entities, key=row_sort_key)

        device_payload: dict = {}
        device_payload["devices"]                         = device_rows
        device_payload["entity_count"]                    = device_entity_count
        device_payload["record_count"]                    = sum(dev["record_count"]                   for dev in device_rows)
        device_payload["recorder_standard_count"]         = sum(dev["recorder_standard_count"]        for dev in device_rows)
        device_payload["recorder_short_count"]            = sum(dev["recorder_short_count"]           for dev in device_rows)
        device_payload["recorder_off_count"]              = sum(dev["recorder_off_count"]             for dev in device_rows)
        device_payload["recorder_standard_record_count"]  = sum(dev["recorder_standard_record_count"] for dev in device_rows)
        device_payload["recorder_short_record_count"]     = sum(dev["recorder_short_record_count"]    for dev in device_rows)
        device_payload["recorder_off_record_count"]       = sum(dev["recorder_off_record_count"]      for dev in device_rows)

        standalones_payload: dict = {}
        standalones_payload["entities"] = standalone_entities
        standalones_payload["record_count"]                   = sum(entity["record_count"]      for entity in standalone_entities)
        standalones_payload["recorder_standard_count"]        = sum(entity["recorder_standard"] for entity in standalone_entities)
        standalones_payload["recorder_short_count"]           = sum(entity["recorder_short"]    for entity in standalone_entities)
        standalones_payload["recorder_off_count"]             = sum(entity["recorder_off"]      for entity in standalone_entities)
        standalones_payload["recorder_standard_record_count"] = sum(entity["record_count"]      for entity in standalone_entities if entity["recorder_standard"])
        standalones_payload["recorder_short_record_count"]    = sum(entity["record_count"]      for entity in standalone_entities if entity["recorder_short"])
        standalones_payload["recorder_off_record_count"]      = sum(entity["record_count"]      for entity in standalone_entities if entity["recorder_off"])

        obsolete_payload: dict = {}
        obsolete_payload["entities"]                       = obsolete_entities
        obsolete_payload["record_count"]                   = sum(entity["record_count"]      for entity in obsolete_entities)
        obsolete_payload["recorder_standard_count"]        = sum(entity["recorder_standard"] for entity in obsolete_entities)
        obsolete_payload["recorder_short_count"]           = sum(entity["recorder_short"]    for entity in obsolete_entities)
        obsolete_payload["recorder_off_count"]             = sum(entity["recorder_off"]      for entity in obsolete_entities)
        obsolete_payload["recorder_standard_record_count"] = sum(entity["record_count"]      for entity in obsolete_entities if entity["recorder_standard"])
        obsolete_payload["recorder_short_record_count"]    = sum(entity["record_count"]      for entity in obsolete_entities if entity["recorder_short"])
        obsolete_payload["recorder_off_record_count"]      = sum(entity["record_count"]      for entity in obsolete_entities if entity["recorder_off"])

        return {
            "purge_keep_days": config["short_retention"]["purge_keep_days"],

            "device_tree":          device_payload,
            "standalone_entities":  standalones_payload,
            "obsolete_entities":    obsolete_payload,

            "total_record_count":   total_record_count,

            "recorder_standard_count": (
                device_payload["recorder_standard_count"] 
                + standalones_payload["recorder_standard_count"]
                + obsolete_payload["recorder_standard_count"] 
            ),
            "recorder_short_count": (
                device_payload["recorder_short_count"] 
                + standalones_payload["recorder_short_count"]
                + obsolete_payload["recorder_short_count"] 
            ),
            "recorder_off_count": (
                device_payload["recorder_off_count"] 
                + standalones_payload["recorder_off_count"]
                + obsolete_payload["recorder_off_count"] 
            ),

            "recorder_standard_record_count": (
                device_payload["recorder_standard_record_count"] 
                + standalones_payload["recorder_standard_record_count"]
                + obsolete_payload["recorder_standard_record_count"] 
            ),
            "recorder_short_record_count": (
                device_payload["recorder_short_record_count"] 
                + standalones_payload["recorder_short_record_count"]
                + obsolete_payload["recorder_short_record_count"] 
            ),
            "recorder_off_record_count": (
                device_payload["recorder_off_record_count"] 
                + standalones_payload["recorder_off_record_count"]
                + obsolete_payload["recorder_off_record_count"] 
            ),
            "version": INTEGRATION_VERSION,
        }

    finally:
        tracing.exit(f)


def _get_entity_counts(hass: HomeAssistant) -> tuple[dict, int]:
    
    f = tracing.enter("_get_entity_counts")    
    try:

        conn = connect_to_db(hass)
        try:
            cursor = conn.cursor()
            t = tracing.start("_get_stats: read entity_ids and counts from states_meta and states")    
            rows = cursor.execute(
                """
                SELECT sm.entity_id, COUNT(*)
                FROM states s
                JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                GROUP BY sm.entity_id
                ORDER BY COUNT(*) DESC
                """
            ).fetchall()
            tracing.stop(t);

            total = sum(count for _, count in rows)
            return { entity_id: count for entity_id, count in rows}, total
        finally:
            conn.close()
    finally:
        tracing.exit(f)
