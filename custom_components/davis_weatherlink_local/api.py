"""Client for the WeatherLink Live Local API."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientError, ClientSession

from .const import REQUEST_TIMEOUT


class WeatherLinkApiError(Exception):
    """Base exception for WeatherLink Local API errors."""


class WeatherLinkConnectionError(WeatherLinkApiError):
    """Raised when the WeatherLink device cannot be reached."""


class WeatherLinkResponseError(WeatherLinkApiError):
    """Raised when the WeatherLink device returns invalid data."""


def normalize_base_url(host: str) -> str:
    """Normalize a hostname, IP address or URL to a base URL."""
    value = host.strip().rstrip("/")
    if not value:
        raise WeatherLinkResponseError("Host must not be empty")

    if "://" not in value:
        value = f"http://{value}"

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WeatherLinkResponseError("Enter a hostname, IP address, or HTTP URL")

    # Ignore any path supplied by the user; the local endpoint is fixed.
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


class WeatherLinkApiClient:
    """Small async client for the WeatherLink Local API."""

    def __init__(self, session: ClientSession, host: str) -> None:
        """Initialize the API client."""
        self._session = session
        self.base_url = normalize_base_url(host)
        self.endpoint = f"{self.base_url}/v1/current_conditions"

    async def async_get_current_conditions(self) -> dict[str, Any]:
        """Return the current conditions JSON document."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(self.endpoint) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
        except (TimeoutError, ClientError, ValueError) as err:
            raise WeatherLinkConnectionError(str(err)) from err

        if not isinstance(payload, dict):
            raise WeatherLinkResponseError("API response is not a JSON object")

        if payload.get("error") not in (None, ""):
            raise WeatherLinkResponseError(f"API returned an error: {payload['error']}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise WeatherLinkResponseError("API response has no data object")

        if data.get("did") in (None, ""):
            raise WeatherLinkResponseError("API response has no device ID")

        conditions = data.get("conditions")
        if not isinstance(conditions, list):
            raise WeatherLinkResponseError("API response has no conditions array")

        return payload
