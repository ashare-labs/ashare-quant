"""Read-only lease guard inside the trusted native writer process."""

import sqlite3
import time
from pathlib import Path

from ashare_quant.contracts import DomainError, Manifest

_control = None


def configure(database, run_id, token):
    global _control
    _control = (Path(database).resolve(), run_id, token)


def check_control(*, allow_cancel=False):
    if _control is None:
        return None  # Explicit standalone synthetic evaluator, outside the host queue.
    path, run_id, token = _control
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["status"] != "running" or row["lease_token"] != token or row["lease_until"] <= time.time():
        raise DomainError("STALE_WORKER", "Native writer no longer owns the accepted lease")
    if row["cancel_requested_at"] and not allow_cancel:
        raise DomainError("NATIVE_CANCEL_REQUESTED", "Native writer must stop at its durable input boundary")
    return Manifest.model_validate_json(row["manifest"])
