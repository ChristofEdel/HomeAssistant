#--------------------------------------------------------------------------------------------------
#
# history_manager/db_stats.py
#
#  - is_sqlite_recorder -> bool | None     Checks if the recorder is using SQLite,
#                                          returns None if unknown.
#
#  - async_analyze_db -> dict              Returns a dict of statistics about the 
#                                          SQLite database. Also stores these stats
#                                          in the component's storage.
#
#  - async_get_table_data -> list[dict]    Returns a list of objects containing name, row count
#                                          and allocated size for each SQLite table.
#
#  Note - async_analyze_db also stores the path to the recorder database in the 
#  component's storage, so that it can be used by other modules
#
#--------------------------------------------------------------------------------------------------
# Code based on the original  "GUI Recorder" by ideaalab, https://github.com/ideaalab/gui-recorder

#--------------------------------------------------------------------------------------------------
# region Imports
#--------------------------------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from homeassistant.core import HomeAssistant

from . import tracing

# endregion
#--------------------------------------------------------------------------------------------------

#--------------------------------------------------------------------------------------------------
# region Database connection and location
#--------------------------------------------------------------------------------------------------

def connect_to_db(hass: HomeAssistant) -> sqlite3.Connection:
    path = _get_db_file_path(hass)
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

def _get_db_file_path(hass: HomeAssistant) -> Path:
    """Return the real on-disk SQLite path from recorder.db_url, falling back to default."""
    db_url = _get_db_url(hass)
    if db_url and db_url.lower().startswith("sqlite"):
        # SQLAlchemy URL formats supported:
        #   sqlite:///relative/path.db
        #   sqlite:////absolute/path.db   (the 4th slash is the root)
        #   sqlite://         -> in-memory (treat as default file)
        try:
            _, _, raw = db_url.partition("://")
            raw = raw.lstrip("/")
            if raw:
                # If the original had 4 slashes ("sqlite:////"), it is absolute.
                if db_url.startswith("sqlite:////"):
                    return Path("/" + raw)
                return Path(hass.config.path(raw))
        except Exception:  # noqa: BLE001
            pass
    return Path(hass.config.path("home-assistant_v2.db"))


def _get_db_url(hass: HomeAssistant) -> str | None:
    """Return the configured recorder db_url, or None if recorder isn't ready."""
    try:
        from homeassistant.helpers.recorder import get_instance
        instance = get_instance(hass)
        return getattr(instance, "db_url", None)
    except Exception:  # noqa: BLE001 - recorder not loaded yet, etc.
        return None
    
# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
#region is_sqlite_recorder
#--------------------------------------------------------------------------------------------------

def is_sqlite_recorder(hass: HomeAssistant) -> bool | None:
    """True if the recorder uses SQLite, False if a different backend, None if unknown."""
    db_url = _get_db_url(hass)
    if not db_url:
        return None
    return db_url.lower().startswith("sqlite")

#endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
# region async_get_db_size
#--------------------------------------------------------------------------------------------------

async def async_get_db_size(hass: HomeAssistant) -> dict:

    try:
        stats = await hass.async_add_executor_job(_get_db_size, hass)
    except Exception as err:  # noqa: BLE001
        stats = {
            "generated_at": None,
            "db_path": str(_get_db_file_path(hass)),
            "db_size_bytes": 0,
            "error": str(err),
        }

    return stats


def _get_db_size(hass: HomeAssistant) -> dict:
    
    f = tracing.enter("_analyze_db")    
    try:

        conn = connect_to_db(hass)
        try:
            db_path = _get_db_file_path(hass)
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "db_path": str(_get_db_file_path(hass)),
                "db_size_bytes": _sqlite_total_size_bytes(db_path),
                "db_reclaimable_bytes": _sqlite_reclaimable_space_bytes(hass),
                "error": None,
            }
        finally:
            conn.close()
    finally:
        tracing.exit(f)



def _sqlite_total_size_bytes(db_path: Path) -> int:
    """Sum the .db, .db-wal and .db-shm sidecar files (WAL mode is the HA default)."""

    f = tracing.enter("_sqlite_total_size_bytes")
    try:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix) if suffix else db_path
            try:
                total += sidecar.stat().st_size
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                pass
        return total
    finally:
        tracing.exit(f)


def _sqlite_reclaimable_space_bytes(hass: HomeAssistant) -> int:

    f = tracing.enter("_sqlite_reclaimable_space_bytes")
    try:
        conn = connect_to_db(hass)
        cursor = conn.cursor()

        page_size = cursor.execute("PRAGMA page_size").fetchone()[0]
        freelist_count = cursor.execute("PRAGMA freelist_count").fetchone()[0]

        free_page_bytes = page_size * freelist_count

        unused_page_bytes = cursor.execute(
            """
            SELECT COALESCE(SUM(unused), 0)
            FROM dbstat
            """
        ).fetchone()[0]

        return free_page_bytes + unused_page_bytes
    
    finally:
        if (conn is not None):
            conn.close()
        tracing.exit(f)

# endregion
#--------------------------------------------------------------------------------------------------


#--------------------------------------------------------------------------------------------------
#region async_get_table_data
#--------------------------------------------------------------------------------------------------

async def async_get_table_data(hass: HomeAssistant) -> list[dict]:
    """Return row count and allocated size for each SQLite table."""
    return await hass.async_add_executor_job(_get_table_data, hass)


def _get_table_data(hass: HomeAssistant) -> list[dict]:
    f = tracing.enter("_get_table_data")

    try:
        conn = connect_to_db(hass)
        try:
            cursor = conn.cursor()

            t = tracing.start("_get_table_data: reading table names")
            tables = [
                row[0]
                for row in cursor.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            ]
            tracing.stop(t)

            # Attribute the storage used by each table's indexes to the table too.
            t = tracing.start("_get_table_data: reading table bytes")
            table_bytes = {
                table_name: bytes_used
                for table_name, bytes_used in cursor.execute(
                    """
                        SELECT objects.tbl_name, SUM(dbstat.pgsize)
                        FROM dbstat
                        JOIN sqlite_master AS objects
                            ON objects.name = dbstat.name
                        WHERE dbstat.aggregate = TRUE
                        AND objects.type IN ('table', 'index')
                        AND objects.tbl_name NOT LIKE 'sqlite_%'
                        GROUP BY objects.tbl_name
                    """
                ).fetchall()
            }
            tracing.stop(t)

            result = []

            t = tracing.start("_get_table_data: reading table row counts")
            for table_name in tables:
                quoted_name = table_name.replace('"', '""')

                rows = cursor.execute(
                    f'SELECT COUNT(*) FROM "{quoted_name}"'
                ).fetchone()[0]

                result.append(
                    {
                        "table": table_name,
                        "rows": rows,
                        "bytes": table_bytes.get(table_name, 0),
                    }
                )
            tracing.stop(t)

            t = tracing.start("_get_table_data: preparing result")
            total_bytes = sum(item["bytes"] for item in result)

            for item in result:
                item["percent"] = (
                    item["bytes"] / total_bytes * 100
                    if total_bytes
                    else 0.0
                )

            result.sort(key=lambda item: item["percent"], reverse=True)
            tracing.stop(t)

            return result

        finally:
            if (conn is not None):
                conn.close()
    finally:
        tracing.exit(f)

# endregion
#--------------------------------------------------------------------------------------------------

