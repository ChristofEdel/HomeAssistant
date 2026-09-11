import logging
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_log_indent = 0


@dataclass
class _Trace:
    text: str
    start_time: float


def _prefix() -> str:
    return " " * (4 * _log_indent)


def enter(text: str) -> _Trace:
    global _log_indent

    _LOGGER.debug("%s--> %s", _prefix(), text)
    _log_indent += 1

    return _Trace(
        text=text,
        start_time=time.perf_counter(),
    )


def exit(trace: _Trace) -> None:
    global _log_indent

    elapsed_ms = (time.perf_counter() - trace.start_time) * 1000

    _log_indent = max(0, _log_indent - 1)

    _LOGGER.debug(
        "%s<-- %s (%.1f ms)",
        _prefix(),
        trace.text,
        elapsed_ms,
    )


def start(text: str) -> _Trace:
    return _Trace(
        text=text,
        start_time=time.perf_counter(),
    )


def stop(trace: _Trace) -> None:
    elapsed_ms = (time.perf_counter() - trace.start_time) * 1000

    _LOGGER.debug(
        "%s%s (%.1f ms)",
        _prefix(),
        trace.text,
        elapsed_ms,
    )


def write(text: str) -> None:
    _LOGGER.debug("%s%s", _prefix(), text)