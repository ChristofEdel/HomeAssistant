#!/usr/bin/env python3

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATABASE_PATH = Path(
    "/usr/local/AppCentral/cloudBackupCenter/database/"
    "cloud_backup_center_log_datebase.db"
)


def main():
    """
    Read the cloud backup database and return the last successful backup
    time along with the success message for every configured backup
    in a JSON array
    """
    db = sqlite3.connect(
        f"file:{DATABASE_PATH}?mode=ro",
        uri=True,
    )

    try:
        rows = db.execute(
            """
            SELECT
                task_id,
                timestamp_of_log,
                name_of_log
            FROM (
                SELECT
                    ID,
                    task_id,
                    timestamp_of_log,
                    name_of_log,
                    ROW_NUMBER() OVER (
                        PARTITION BY task_id
                        ORDER BY timestamp_of_log DESC, ID DESC
                    ) AS rn
                FROM cloud_backup_center_sql_database
                WHERE status_of_log = 'SUCCESS'
                  AND name_of_log LIKE '%finished successfully.'
            )
            WHERE rn = 1
            ORDER BY task_id
            """
        ).fetchall()
    finally:
        db.close()

    result = [
        {
            "id": task_id,
            "timestamp": datetime.fromtimestamp(
                timestamp_of_log,
                timezone.utc,
            ).isoformat(),
            "message": name_of_log,
        }
        for task_id, timestamp_of_log, name_of_log in rows
    ]

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
