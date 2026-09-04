"""CSV input and timestamp parsing for History Import."""

#--------------------------------------------------------------------------------
#region Imports
#--------------------------------------------------------------------------------

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Final
from zoneinfo import ZoneInfo

from homeassistant.const import MAX_LENGTH_STATE_STATE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region Options for time treatment
#--------------------------------------------------------------------------------

TIME_ZONE_UTC: Final = "UTC"
TIME_ZONE_LOCAL: Final = "local"

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region Regex for time formats
#--------------------------------------------------------------------------------

_UNIX_RE = re.compile(r"^[+-]?\d+(?:\.\d{1,6})?$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region Supporting classes for errors and data points
#--------------------------------------------------------------------------------

class CsvValidationError(ServiceValidationError):
    """Raised when a CSV row is invalid."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One validated input sample."""

    line: int
    timestamp: float
    state: str

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region main function: read_and_validate_csv
#--------------------------------------------------------------------------------

def read_and_validate_csv(
    path: Path,
    time_zone: str,
    local_tz: ZoneInfo,
) -> tuple[Sample, ...]:
    """Read and validate the complete CSV before any database work."""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as err:
        raise ServiceValidationError(f"Cannot open CSV file {path}: {err}") from err

    samples: list[Sample] = []
    previous_timestamp: float | None = None
    first_nonblank_seen = False
    last_line = 0

    try:
        with handle:
            reader = csv.reader(handle, strict=True)
            try:
                for row in reader:
                    line_number = reader.line_num
                    last_line = line_number
                    if not row or all(not cell.strip() for cell in row):
                        continue

                    if not first_nonblank_seen:
                        first_nonblank_seen = True

                        if len(row) >= 2:
                            timestamp_text = row[0].strip()
                            value_text = row[1].strip()

                            timestamp_valid = True
                            value_valid = True

                            try:
                                _parse_timestamp(timestamp_text, time_zone, local_tz)
                            except ValueError:
                                timestamp_valid = False

                            try:
                                value = float(value_text)
                                value_valid = math.isfinite(value)
                            except ValueError:
                                value_valid = False

                            # If neither column resembles valid import data,
                            # interpret the first row as a header.
                            if not timestamp_valid and not value_valid:
                                continue

                    if len(row) < 2:
                        raise CsvValidationError(
                            f"CSV line {line_number}: expected at least 2 columns"
                        )

                    timestamp_text = row[0].strip()
                    value_text = row[1].strip()

                    try:
                        timestamp = _parse_timestamp(
                            timestamp_text, time_zone, local_tz
                        )
                    except ValueError as err:
                        raise CsvValidationError(
                            f"CSV line {line_number}: invalid timestamp "
                            f"{timestamp_text!r}: {err}"
                        ) from err

                    if not value_text:
                        raise CsvValidationError(
                            f"CSV line {line_number}: value is empty"
                        )

                    try:
                        value = float(value_text)
                    except ValueError as err:
                        raise CsvValidationError(
                            f"CSV line {line_number}: invalid numeric value "
                            f"{value_text!r}"
                        ) from err

                    if not math.isfinite(value):
                        raise CsvValidationError(
                            f"CSV line {line_number}: value must be finite"
                        )
                    if len(value_text) > MAX_LENGTH_STATE_STATE:
                        raise CsvValidationError(
                            f"CSV line {line_number}: value exceeds Home Assistant "
                            "state length limit"
                        )

                    if (
                        previous_timestamp is not None
                        and timestamp <= previous_timestamp
                    ):
                        raise CsvValidationError(
                            f"CSV line {line_number}: timestamps must be strictly "
                            "increasing"
                        )

                    samples.append(
                        Sample(
                            line=line_number,
                            timestamp=timestamp,
                            state=value_text,
                        )
                    )
                    previous_timestamp = timestamp
            except csv.Error as err:
                raise CsvValidationError(
                    f"CSV line {reader.line_num}: invalid CSV: {err}"
                ) from err
    except UnicodeDecodeError as err:
        # Text decoding errors are reported against the next physical CSV line.
        raise CsvValidationError(
            f"CSV line {last_line + 1}: file is not valid UTF-8"
        ) from err

    if not samples:
        raise CsvValidationError("CSV contains no data rows")

    return tuple(samples)

#endregion
#--------------------------------------------------------------------------------

#--------------------------------------------------------------------------------
#region Supporting functions for timestamp interpretation and file path resolution
# - resolve_file_path
# - _parse_timestamp
# - _localize_strict
#--------------------------------------------------------------------------------

def resolve_file_path(hass: HomeAssistant, file_name: str) -> Path:
    path = Path(file_name).expanduser()
    if not path.is_absolute():
        path = Path(hass.config.path(file_name))
    return path


def _parse_timestamp(text: str, time_zone: str, local_tz: ZoneInfo) -> float:
    """Parse one accepted timestamp to UTC Unix seconds."""
    if _UNIX_RE.fullmatch(text):
        try:
            timestamp_decimal = Decimal(text)
        except InvalidOperation as err:
            raise ValueError("invalid Unix timestamp") from err

        if not timestamp_decimal.is_finite():
            raise ValueError("Unix timestamp must be finite")

        timestamp = float(timestamp_decimal)
        try:
            datetime.fromtimestamp(timestamp, UTC)
        except (OverflowError, OSError, ValueError) as err:
            raise ValueError("Unix timestamp is outside the supported range") from err
        return timestamp

    if not _DATETIME_RE.fullmatch(text):
        raise ValueError("unsupported timestamp format")

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as err:
        raise ValueError("invalid date/time") from err

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).timestamp()

    if time_zone == TIME_ZONE_UTC:
        return parsed.replace(tzinfo=UTC).timestamp()

    aware = _localize_strict(parsed, local_tz)
    return aware.astimezone(UTC).timestamp()


def _localize_strict(value: datetime, tz: ZoneInfo) -> datetime:
    """Attach a local timezone while rejecting DST gaps."""
    fold0 = value.replace(tzinfo=tz, fold=0)
    fold1 = value.replace(tzinfo=tz, fold=1)

    valid0 = (
        fold0.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value
    )
    valid1 = (
        fold1.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value
    )

    if not valid0 and not valid1:
        raise ValueError("nonexistent local time caused by a DST transition")

    return fold0 if valid0 else fold1

#endregion
#--------------------------------------------------------------------------------
