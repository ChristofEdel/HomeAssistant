"""Base entity for Davis WeatherLink Local."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import WeatherLinkCoordinator
from .device import device_info


class DavisWeatherLinkEntity(CoordinatorEntity[WeatherLinkCoordinator]):
    """Base coordinator entity attached to a WeatherLink device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WeatherLinkCoordinator,
        unique_id: str,
        device_key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info(coordinator, device_key)
