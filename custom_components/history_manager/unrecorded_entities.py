#--------------------------------------------------------------------------------------------------
#
# history_manager/unrecorded_entities.py
#
# get_all_short_retention_entity_ids     Get the entity ids of all entities which are excluded
#                                        from the recorder ("unrecorded entities")
#
# async_purge_unrecorded_entities        Call "purge" for all unrecorded entities with the
#                                        purge_keep_days = 0
#
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

from fnmatch import fnmatchcase
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance as get_recorder_instance
from .storage import get_runtime_storage

# endregion
#--------------------------------------------------------------------------------------------------


def get_all_unrecorded_entity_ids(hass: HomeAssistant) -> list[str]:
    entity_registry = er.async_get(hass)
    recorder = get_recorder_instance(hass)
    result: list[str] = []
    entity_filter = recorder.entity_filter
    if entity_filter is None:
        return result

    # get all live entities from the entity registry
    entity_ids = set(hass.states.async_entity_ids()) | {
        entity.entity_id for entity in entity_registry.entities.values()
    }

    # add all entities from the entity registry which are in the recorder filter
    for entity_id in entity_ids:
         recorded = entity_filter(entity_id)
         if not recorded:
            result.append(entity_id)

    return result


async def async_purge_unrecorded_entities(
    hass: HomeAssistant
) -> None:

    entity_ids = get_all_unrecorded_entity_ids(hass)

    if not entity_ids:
        return


    await hass.services.async_call(
        "recorder",
        "purge_entities",
        {
            "entity_id": entity_ids,
            "keep_days": 0,
        },
        blocking=True,
    )

