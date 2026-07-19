"""Device registry helpers for Davis WeatherLink Local."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DEVICE_CONSOLE,
    DEVICE_HUB,
    DEVICE_OUTDOOR,
    DEVICE_SOIL,
    DOMAIN,
)
from .coordinator import WeatherLinkCoordinator


def device_identifier(did: str, device_key: str) -> tuple[str, str]:
    """Return a stable Home Assistant device identifier."""
    if device_key == DEVICE_HUB:
        return (DOMAIN, did)
    return (DOMAIN, f"{did}:{device_key}")


def device_info(
    coordinator: WeatherLinkCoordinator,
    device_key: str,
) -> DeviceInfo:
    """Return device metadata and parent/child topology."""
    did = coordinator.data.did
    hub_identifier = device_identifier(did, DEVICE_HUB)

    if device_key == DEVICE_HUB:
        return DeviceInfo(
            identifiers={hub_identifier},
            name="Davis WeatherLink",
            manufacturer="Davis Instruments",
            model="WeatherLink Local API",
            serial_number=did,
            configuration_url=coordinator.api.base_url,
        )

    child_metadata = {
        DEVICE_OUTDOOR: ("Outdoor ISS", "Integrated Sensor Suite", "TXID 1"),
        DEVICE_SOIL: ("Soil Station", "Soil Moisture/Temperature Station", "TXID 5"),
        DEVICE_CONSOLE: ("Console", "WeatherLink Live", "Console"),
    }
    name, model, serial_number = child_metadata[device_key]

    return DeviceInfo(
        identifiers={device_identifier(did, device_key)},
        name=name,
        manufacturer="Davis Instruments",
        model=model,
        serial_number=serial_number,
        via_device=hub_identifier,
    )
