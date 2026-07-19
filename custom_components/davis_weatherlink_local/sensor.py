"""Sensor entities for Davis WeatherLink Local."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    DEGREE,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_CONSOLE,
    DEVICE_HUB,
    DEVICE_OUTDOOR,
    DEVICE_SOIL,
    SOURCE_BAROMETER,
    SOURCE_INDOOR,
    SOURCE_OUTDOOR,
    SOURCE_ROOT,
    SOURCE_SOIL,
)
from .coordinator import WeatherLinkCoordinator
from .entity import DavisWeatherLinkEntity

ValueTransform = Callable[[Any], Any]


def _identity(value: Any) -> Any:
    return value


def _fahrenheit_to_celsius(value: Any) -> float:
    return round((float(value) - 32.0) * 5.0 / 9.0, 1)


def _inhg_to_hpa(value: Any) -> float:
    return round(float(value) * 33.8638866667, 1)


def _timestamp(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def _receiver_state(value: Any) -> str:
    return {
        0: "Ok",
        1: "Unreliable",
        2: "Disconnected",
    }.get(int(value), "Unknown")


@dataclass(frozen=True, kw_only=True)
class DavisSensorDescription(SensorEntityDescription):
    """Describe one WeatherLink sensor."""

    source: str
    field: str
    device_key: str
    transform: ValueTransform = _identity


SENSORS: tuple[DavisSensorDescription, ...] = (
    DavisSensorDescription(
        key="davis_weatherlink_last_update",
        translation_key="last_update",
        source=SOURCE_ROOT,
        field="ts",
        device_key=DEVICE_HUB,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    DavisSensorDescription(
        key="davis_outdoor_temperature",
        translation_key="temperature",
        source=SOURCE_OUTDOOR,
        field="temp",
        device_key=DEVICE_OUTDOOR,
        transform=_fahrenheit_to_celsius,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_outdoor_humidity",
        translation_key="humidity",
        source=SOURCE_OUTDOOR,
        field="hum",
        device_key=DEVICE_OUTDOOR,
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DavisSensorDescription(
        key="davis_outdoor_dew_point",
        translation_key="dew_point",
        source=SOURCE_OUTDOOR,
        field="dew_point",
        device_key=DEVICE_OUTDOOR,
        transform=_fahrenheit_to_celsius,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_outdoor_wet_bulb_temperature",
        translation_key="wet_bulb_temperature",
        source=SOURCE_OUTDOOR,
        field="wet_bulb",
        device_key=DEVICE_OUTDOOR,
        transform=_fahrenheit_to_celsius,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_wind_speed",
        translation_key="wind_speed",
        source=SOURCE_OUTDOOR,
        field="wind_speed_last",
        device_key=DEVICE_OUTDOOR,
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DavisSensorDescription(
        key="davis_wind_direction",
        translation_key="wind_direction",
        source=SOURCE_OUTDOOR,
        field="wind_dir_last",
        device_key=DEVICE_OUTDOOR,
        device_class=SensorDeviceClass.WIND_DIRECTION,
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT_ANGLE,
    ),
    DavisSensorDescription(
        key="davis_wind_speed_1_minute_average",
        translation_key="wind_speed_1_minute_average",
        source=SOURCE_OUTDOOR,
        field="wind_speed_avg_last_1_min",
        device_key=DEVICE_OUTDOOR,
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DavisSensorDescription(
        key="davis_wind_direction_1_minute_average",
        translation_key="wind_direction_1_minute_average",
        source=SOURCE_OUTDOOR,
        field="wind_dir_scalar_avg_last_1_min",
        device_key=DEVICE_OUTDOOR,
        device_class=SensorDeviceClass.WIND_DIRECTION,
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT_ANGLE,
    ),
    DavisSensorDescription(
        key="davis_rain_collector_type",
        translation_key="rain_collector_type",
        source=SOURCE_OUTDOOR,
        field="rain_size",
        device_key=DEVICE_OUTDOOR,
    ),
    DavisSensorDescription(
        key="davis_rain_rate",
        translation_key="rain_rate",
        source=SOURCE_OUTDOOR,
        field="rain_rate_last",
        device_key=DEVICE_OUTDOOR,
        native_unit_of_measurement="counts/h",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-rainy",
    ),
    DavisSensorDescription(
        key="davis_highest_rain_rate",
        translation_key="highest_rain_rate",
        source=SOURCE_OUTDOOR,
        field="rain_rate_hi",
        device_key=DEVICE_OUTDOOR,
        native_unit_of_measurement="counts/h",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-pouring",
    ),
    DavisSensorDescription(
        key="davis_rainfall_last_15_minutes",
        translation_key="rainfall_last_15_minutes",
        source=SOURCE_OUTDOOR,
        field="rainfall_last_15_min",
        device_key=DEVICE_OUTDOOR,
        native_unit_of_measurement="counts",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-rainy",
    ),
    DavisSensorDescription(
        key="davis_highest_rain_rate_last_15_minutes",
        translation_key="highest_rain_rate_last_15_minutes",
        source=SOURCE_OUTDOOR,
        field="rain_rate_hi_last_15_min",
        device_key=DEVICE_OUTDOOR,
        native_unit_of_measurement="counts/h",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-pouring",
    ),
    DavisSensorDescription(
        key="davis_daily_rainfall",
        translation_key="daily_rainfall",
        source=SOURCE_OUTDOOR,
        field="rainfall_daily",
        device_key=DEVICE_OUTDOOR,
        native_unit_of_measurement="counts",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:weather-rainy",
    ),
    DavisSensorDescription(
        key="davis_sreceiver_state",
        translation_key="receiver_state",
        source=SOURCE_OUTDOOR,
        field="rx_state",
        device_key=DEVICE_OUTDOOR,
        transform=_receiver_state,
        icon="mdi:radio-tower",
    ),
    *tuple(
        DavisSensorDescription(
            key=f"davis_soil_temperature_{channel}",
            translation_key=f"soil_temperature_{channel}",
            source=SOURCE_SOIL,
            field=f"temp_{channel}",
            device_key=DEVICE_SOIL,
            transform=_fahrenheit_to_celsius,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        )
        for channel in range(1, 5)
    ),
    *tuple(
        DavisSensorDescription(
            key=f"davis_soil_moisture_{channel}",
            translation_key=f"soil_moisture_{channel}",
            source=SOURCE_SOIL,
            field=f"moist_soil_{channel}",
            device_key=DEVICE_SOIL,
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.KPA,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for channel in range(1, 5)
    ),
    DavisSensorDescription(
        key="davis_soil_receiver_state",
        translation_key="receiver_state",
        source=SOURCE_SOIL,
        field="rx_state",
        device_key=DEVICE_SOIL,
        transform=_receiver_state,
        icon="mdi:radio-tower",
    ),
    DavisSensorDescription(
        key="davis_indoor_temperature",
        translation_key="temperature",
        source=SOURCE_INDOOR,
        field="temp_in",
        device_key=DEVICE_CONSOLE,
        transform=_fahrenheit_to_celsius,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_indoor_humidity",
        translation_key="humidity",
        source=SOURCE_INDOOR,
        field="hum_in",
        device_key=DEVICE_CONSOLE,
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DavisSensorDescription(
        key="davis_indoor_dew_point",
        translation_key="dew_point",
        source=SOURCE_INDOOR,
        field="dew_point_in",
        device_key=DEVICE_CONSOLE,
        transform=_fahrenheit_to_celsius,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_sea_level_pressure",
        translation_key="sea_level_pressure",
        source=SOURCE_BAROMETER,
        field="bar_sea_level",
        device_key=DEVICE_CONSOLE,
        transform=_inhg_to_hpa,
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DavisSensorDescription(
        key="davis_absolute_pressure",
        translation_key="absolute_pressure",
        source=SOURCE_BAROMETER,
        field="bar_absolute",
        device_key=DEVICE_CONSOLE,
        transform=_inhg_to_hpa,
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WeatherLink sensors."""
    coordinator: WeatherLinkCoordinator = entry.runtime_data
    async_add_entities(
        DavisWeatherLinkSensor(coordinator, description)
        for description in SENSORS
    )


class DavisWeatherLinkSensor(DavisWeatherLinkEntity, SensorEntity):
    """A sensor backed by the shared WeatherLink coordinator."""

    entity_description: DavisSensorDescription

    def __init__(
        self,
        coordinator: WeatherLinkCoordinator,
        description: DavisSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key, description.device_key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return whether this field is present in the latest response."""
        return (
            super().available
            and self.coordinator.data.value(
                self.entity_description.source,
                self.entity_description.field,
            )
            is not None
        )

    @property
    def native_value(self) -> Any:
        """Return the converted sensor value."""
        value = self.coordinator.data.value(
            self.entity_description.source,
            self.entity_description.field,
        )
        if value is None:
            return None
        try:
            return self.entity_description.transform(value)
        except (TypeError, ValueError, OverflowError):
            return None
