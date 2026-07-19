"""Constants for the Davis WeatherLink Local integration."""

from typing import Final

DOMAIN: Final = "davis_weatherlink_local"

CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 10
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 3600
REQUEST_TIMEOUT: Final = 10

SOURCE_ROOT: Final = "root"
SOURCE_OUTDOOR: Final = "outdoor"
SOURCE_SOIL: Final = "soil"
SOURCE_INDOOR: Final = "indoor"
SOURCE_BAROMETER: Final = "barometer"

DEVICE_HUB: Final = "hub"
DEVICE_OUTDOOR: Final = "outdoor"
DEVICE_SOIL: Final = "soil"
DEVICE_CONSOLE: Final = "console"

OUTDOOR_TXID: Final = 1
SOIL_TXID: Final = 5
INDOOR_DATA_STRUCTURE_TYPE: Final = 4
BAROMETER_DATA_STRUCTURE_TYPE: Final = 3
