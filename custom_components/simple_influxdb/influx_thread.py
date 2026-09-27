import queue
import logging
import threading
import time

from collections.abc import Callable
from contextlib import suppress
from typing import Any, override

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.config_entries import ConfigEntry

from .influx_connection import InfluxClient

from .const import (
    DOMAIN,
    BATCH_TIMEOUT,
    BATCH_BUFFER_SIZE,
    QUEUE_BACKLOG_SECONDS,
    RETRY_DELAY
)

_LOGGER = logging.getLogger(__name__)


#--------------------------------------------------------------------------------------------------
# region class InfluxThread - main receive event / write to InfluxDB processing loop
#--------------------------------------------------------------------------------------------------
#
# Processing pattern:
#

class InfluxThread(threading.Thread):
    """The main HomeAssistant --> InfluxDB transfer thread class."""

    
    _influx_client: InfluxClient            # The InfluxClient to which we write the data we receive
    _event_to_json: Callable[               # A callable invoked for every event, which should 
        [Event],                            # translate the event to JSON for posting to the
        list[dict[str, Any]] | None         # influx_client
    ]
    _queue: queue.SimpleQueue[              # The queue in which we queue the events for processing
        threading.Event 
        | tuple[float, Event] 
        | None
    ]
    _max_tries: int                         # How often we retry if we fail to write
    _write_errors: int                      # the number of write errors we encountered
    _shutdown: bool                         # flag indicating the thread should shut down

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        influx_client: InfluxClient,
        event_to_json: Callable[[Event], list[dict[str, Any]] | None],
        max_tries: int,
    ) -> None:
        """Initialize the HomeAssistant event listener."""

        # initialise the object
        threading.Thread.__init__(self, name=DOMAIN)
        self._queue = queue.SimpleQueue()
        self._influx_client = influx_client
        self._event_to_json = event_to_json
        self._max_tries = max_tries
        self._write_errors = 0
        self._shutdown = False

        entry.async_on_unload(
            hass.bus.async_listen(EVENT_STATE_CHANGED, self._event_listener)
        )

    @override
    def run(self):
        """Main processing loop: receive (batched) events and send them to InfluxDB."""
        while not self._shutdown:
            _, json = self._get_event_batch_json()
            if json:
                self._write_to_influxdb(json)

    def shutdown(self) -> None:
        """Shutdown the influx thread."""
        self._queue.put(None)                # Queue 'None' as event, which initiates shutdown
        self.join()                         # wait for the thread to finish
        self._influx_client.close()



    @callback
    def _event_listener(self, event):
        """Called when a new messages on the bus has arreived - queue it for processing."""
        item = (time.monotonic(), event)
        self._queue.put(item)

    @staticmethod
    def batch_timeout():
        """Return number of seconds to wait for more events."""
        return BATCH_TIMEOUT

    def _get_event_batch_json(self):
        """Return a batch of events formatted for writing."""
        queue_seconds = QUEUE_BACKLOG_SECONDS + self._max_tries * RETRY_DELAY

        count = 0
        json = []

        dropped = 0

        with suppress(queue.Empty):
            while len(json) < BATCH_BUFFER_SIZE and not self._shutdown:
                timeout = None if count == 0 else self.batch_timeout()
                item = self._queue.get(timeout=timeout)
                count += 1

                if item is None:
                    self._shutdown = True
                elif type(item) is tuple:
                    timestamp, event = item
                    age = time.monotonic() - timestamp

                    if age < queue_seconds:
                        if event_json := self._event_to_json(event):
                            json.extend(event_json)
                    else:
                        dropped += 1
                elif isinstance(item, threading.Event):
                    item.set()

        if dropped:
            _LOGGER.warning("Catching up, dropped %d old events.", dropped)

        return count, json

    def _write_to_influxdb(self, json):
        """Write preprocessed events to influxdb, with retry."""
        for retry in range(self._max_tries + 1):
            try:
                self._influx_client.write(json)

                if self._write_errors:
                    _LOGGER.error("Resumed, lost %d events.", self._write_errors)
                    self._write_errors = 0

                _LOGGER.debug("Wrote %d events.", len(json))
                break
            except ValueError as err:
                _LOGGER.error(err)
                break
            except ConnectionError as err:
                if retry < self._max_tries:
                    time.sleep(RETRY_DELAY)
                else:
                    if not self._write_errors:
                        _LOGGER.error(err)
                    self._write_errors += len(json)

# endregion
#--------------------------------------------------------------------------------------------------