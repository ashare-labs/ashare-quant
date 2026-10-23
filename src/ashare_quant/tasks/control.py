"""Child-side queue lease, cancellation and timeout supervision."""
import os
import sqlite3
import time
from pathlib import Path


def watch_control(control, execution_seconds):
    """Stop only this process when ownership/cancellation changes, including orphaned workers."""
    started = time.monotonic()
    while True:
        try:
            uri = Path(control["database"]).as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as db:
                row = db.execute(
                    "SELECT status,lease_token,lease_until,cancellation_requested_at FROM tasks WHERE job_id=?",
                    (control["job_id"],),
                ).fetchone()
            if (
                not row
                or row[0] not in {"running", "cancelling"}
                or row[1] != control["token"]
                or row[2] <= time.time()
            ):
                os._exit(74)
            if row[3]:
                os._exit(73)
            if time.monotonic() - started > execution_seconds:
                os._exit(75)
        except Exception:
            os._exit(74)
        time.sleep(0.1)
