#--------------------------------------------------------------------------------------------------
#
# history_manager/short_retention_entities.py
#
# is_short_retention_entity              Test if the given entity id is configured
#                                        as "short retention"
#
# get_all_short_retention_entity_ids     Get the entity ids of all entities which are configured
#                                        as "short retention"
#
# async_purge_short_entities             Call "purge" for all short retention entities with the
#                                        configured purge_keep_days
#
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

from fnmatch import fnmatchcase
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from .storage import get_runtime_storage
from . import tracing

# endregion
#--------------------------------------------------------------------------------------------------


def is_short_retention_entity(
    short_retention_config: dict,
    entity_id: str,
) -> bool:
    """Return whether entity_id is covered by short retention."""

    if entity_id in short_retention_config["except"]:
        return False

    if any(
        fnmatchcase(entity_id, pattern)
        for pattern in short_retention_config["except_globs"]
    ):
        return False

    if entity_id in short_retention_config["entities"]:
        return True

    return any(
        fnmatchcase(entity_id, pattern)
        for pattern in short_retention_config["entity_globs"]
    )


def get_all_short_retention_entity_ids(hass: HomeAssistant) -> list[str]:
    entity_registry = er.async_get(hass)
    runtime_storage = get_runtime_storage(hass)
    config = runtime_storage.config
    config_short_retention = config["short_retention"]

    processed_entity_ids: set[str] = set()
    result: list[str] = []

    # add all matching entities from the entity registry
    for entity_entry in entity_registry.entities.values():
         entity_id = entity_entry.entity_id
         if not is_short_retention_entity(config_short_retention, entity_id):
             continue
         if entity_id in processed_entity_ids:
             continue
         processed_entity_ids.add(entity_id)
         result.append(entity_id)

    return result


async def async_purge_short_entities(
    hass: HomeAssistant
) -> None:
    runtime_storage = get_runtime_storage(hass)
    config = runtime_storage.config
    config_short_retention = config["short_retention"]
    retention_days = config_short_retention["purge_keep_days"]

    entity_ids = get_all_short_retention_entity_ids(hass)

    if not entity_ids:
        return


    await hass.services.async_call(
        "recorder",
        "purge_entities",
        {
            "entity_id": entity_ids,
            "keep_days": retention_days,
        },
        blocking=True,
    )

