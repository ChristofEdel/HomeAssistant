"""Data update coordinator for Davis WeatherLink Local."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WeatherLinkApiClient, WeatherLinkApiError
from .const import (
    BAROMETER_DATA_STRUCTURE_TYPE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    INDOOR_DATA_STRUCTURE_TYPE,
    OUTDOOR_TXID,
    SOIL_TXID,
    SOURCE_BAROMETER,
    SOURCE_INDOOR,
    SOURCE_OUTDOOR,
    SOURCE_ROOT,
    SOURCE_SOIL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeatherLinkData:
    """Parsed WeatherLink API data."""

    did: str
    timestamp: int | float | None
    root: dict[str, Any]
    by_txid: dict[int, dict[str, Any]]
    by_structure: dict[int, dict[str, Any]]

    def source(self, source: str) -> dict[str, Any]:
        """Return a logical source object."""
        if source == SOURCE_ROOT:
            return self.root
        if source == SOURCE_OUTDOOR:
            return self.by_txid.get(OUTDOOR_TXID, {})
        if source == SOURCE_SOIL:
            return self.by_txid.get(SOIL_TXID, {})
        if source == SOURCE_INDOOR:
            return self.by_structure.get(INDOOR_DATA_STRUCTURE_TYPE, {})
        if source == SOURCE_BAROMETER:
            return self.by_structure.get(BAROMETER_DATA_STRUCTURE_TYPE, {})
        return {}

    def value(self, source: str, field: str) -> Any:
        """Return one field from a logical source."""
        return self.source(source).get(field)


class WeatherLinkCoordinator(DataUpdateCoordinator[WeatherLinkData]):
    """Coordinate a single current_conditions poll for every entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WeatherLinkApiClient,
    ) -> None:
        """Initialize the coordinator."""
        scan_interval = int(entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name="Davis WeatherLink Local",
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
            always_update=False,
        )
        self.api = api

    async def _async_update_data(self) -> WeatherLinkData:
        """Fetch and parse current conditions."""
        try:
            payload = await self.api.async_get_current_conditions()
        except WeatherLinkApiError as err:
            raise UpdateFailed(f"Error communicating with WeatherLink: {err}") from err

        data = payload["data"]
        by_txid: dict[int, dict[str, Any]] = {}
        by_structure: dict[int, dict[str, Any]] = {}

        for condition in data["conditions"]:
            if not isinstance(condition, dict):
                continue

            txid = condition.get("txid")
            if isinstance(txid, int):
                by_txid[txid] = condition

            structure_type = condition.get("data_structure_type")
            if isinstance(structure_type, int):
                by_structure[structure_type] = condition

        return WeatherLinkData(
            did=str(data["did"]),
            timestamp=data.get("ts"),
            root=data,
            by_txid=by_txid,
            by_structure=by_structure,
        )
