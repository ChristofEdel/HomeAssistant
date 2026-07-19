"""Binary sensor entities for Davis WeatherLink Local."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEVICE_OUTDOOR, DEVICE_SOIL, SOURCE_OUTDOOR, SOURCE_SOIL
from .coordinator import WeatherLinkCoordinator
from .entity import DavisWeatherLinkEntity


@dataclass(frozen=True, kw_only=True)
class DavisBinarySensorDescription(BinarySensorEntityDescription):
    """Describe one WeatherLink binary sensor."""

    source: str
    field: str
    device_key: str


BINARY_SENSORS: tuple[DavisBinarySensorDescription, ...] = (
    DavisBinarySensorDescription(
        key="davis_outdoor_transmitter_battery_low",
        translation_key="transmitter_battery_low",
        source=SOURCE_OUTDOOR,
        field="trans_battery_flag",
        device_key=DEVICE_OUTDOOR,
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
    DavisBinarySensorDescription(
        key="davis_soil_transmitter_battery_low",
        translation_key="transmitter_battery_low",
        source=SOURCE_SOIL,
        field="trans_battery_flag",
        device_key=DEVICE_SOIL,
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WeatherLink binary sensors."""
    coordinator: WeatherLinkCoordinator = entry.runtime_data
    async_add_entities(
        DavisWeatherLinkBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class DavisWeatherLinkBinarySensor(DavisWeatherLinkEntity, BinarySensorEntity):
    """A binary sensor backed by the shared WeatherLink coordinator."""

    entity_description: DavisBinarySensorDescription

    def __init__(
        self,
        coordinator: WeatherLinkCoordinator,
        description: DavisBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, description.key, description.device_key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return whether the battery flag is present."""
        return (
            super().available
            and self.coordinator.data.value(
                self.entity_description.source,
                self.entity_description.field,
            )
            is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return true when Davis reports a low transmitter battery."""
        value = self.coordinator.data.value(
            self.entity_description.source,
            self.entity_description.field,
        )
        if value is None:
            return None
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return None
