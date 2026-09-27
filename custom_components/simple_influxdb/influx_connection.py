from typing import Any, override
from dataclasses import dataclass

from collections.abc import Callable
from contextlib import suppress
from influxdb_client.client.influxdb_client import InfluxDBClient as InfluxDBClientV2
from influxdb_client.client.write_api import ASYNCHRONOUS, SYNCHRONOUS
from influxdb_client.rest import ApiException
import urllib3.exceptions

from homeassistant.const import (
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
)

from .const import (
    CONF_PRECISION,
    CONF_SSL_CA_CERT,
    CONF_BUCKET,
    TIMEOUT
)

@dataclass
class InfluxClient:
    """An InfluxDB client wrapper"""
    data_repositories: list[str]
    write: Callable[[list[dict[str, Any]]], None]
    query: Callable[[str, str], list[Any]]
    close: Callable[[], None]


def get_influx_connection(  # noqa: C901
    conf, test_write=False, test_read=False
) -> InfluxClient:
    """Create the correct influx connection for the API version."""
    kwargs: dict[str, Any] = {
        CONF_TIMEOUT: TIMEOUT,
    }
    precision = conf.get(CONF_PRECISION)

    kwargs[CONF_TIMEOUT] = TIMEOUT * 1000
    kwargs[CONF_URL] = conf[CONF_URL]
    kwargs[CONF_TOKEN] = conf[CONF_TOKEN]
    kwargs["org"] = "dummy"
    kwargs[CONF_VERIFY_SSL] = conf[CONF_VERIFY_SSL]
    if (cert := conf.get(CONF_SSL_CA_CERT)) is not None:
        kwargs[CONF_SSL_CA_CERT] = cert
    bucket = conf.get(CONF_BUCKET)
    influx = InfluxDBClientV2(**kwargs)
    query_api = influx.query_api()
    initial_write_mode = SYNCHRONOUS if test_write else ASYNCHRONOUS
    write_api = influx.write_api(write_options=initial_write_mode)

    def write_v2(json):
        """Write data to V2 influx."""
        data = {"bucket": bucket, "record": json}

        if precision is not None:
            data["write_precision"] = precision

        try:
            write_api.write(**data)
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise ConnectionError(CONNECTION_ERROR % exc) from exc
        except ApiException as exc:
            if exc.status == CODE_INVALID_INPUTS:
                raise ValueError(WRITE_ERROR % (json, exc)) from exc
            raise ConnectionError(CLIENT_ERROR_V2 % exc) from exc

    def query_v2(query, _=None):
        """Query V2 influx."""
        try:
            return query_api.query(query)
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise ConnectionError(CONNECTION_ERROR % exc) from exc
        except ApiException as exc:
            if exc.status == CODE_INVALID_INPUTS:
                raise ValueError(QUERY_ERROR % (query, exc)) from exc
            raise ConnectionError(CLIENT_ERROR_V2 % exc) from exc

    def close_v2():
        """Close V2 influx client."""
        influx.close()

    buckets = []
    if test_write:
        # Try to write b"" to influx. If we can connect and creds are valid
        # Then invalid inputs is returned. Anything else is a broken config
        with suppress(ValueError):
            write_v2(b"")
        write_api = influx.write_api(write_options=ASYNCHRONOUS)

    if test_read:
        tables = query_v2("buckets()")
        if tables and tables[0].records:
            buckets = [bucket.values["name"] for bucket in tables[0].records]
        else:
            buckets = []

    return InfluxClient(buckets, write_v2, query_v2, close_v2)

CODE_INVALID_INPUTS = 400

CONNECTION_ERROR = (
    "Cannot connect to InfluxDB due to '%s'. "
    "Please check that the provided connection details (host, port, etc.) are correct "
    "and that your InfluxDB server is running and accessible."
)

WRITE_ERROR = "Could not write '%s' to influx due to '%s'."

CLIENT_ERROR_V2 = (
    "InfluxDB bucket is not accessible due to '%s'. "
    "Please check that the bucket, org and token are correct and "
    "that the token has the correct permissions set."
)

QUERY_ERROR = (
    "Could not execute query '%s' due to '%s'. Check the syntax of your query."
)