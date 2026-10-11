"""Pinned, read-only owner transport; query errors come from the running AD API."""

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path

from ashare_quant.diagnostics.runner import digest, readonly


def execute(payload):
    sys.addaudithook(readonly)
    pin = json.loads(Path(__file__).with_name("calendar-owner-pin.json").read_text())
    for name, expected in pin["runtime"].items():
        actual = platform.python_version() if name == "python" else importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError("CALENDAR_OWNER_RUNTIME_MISMATCH:" + name)
    root = Path(importlib.util.find_spec("ashare_data").origin).parent
    inventory = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".json", ".sha256"}
    }
    if inventory != pin["files"] or digest(inventory) != pin["content_sha256"]:
        raise RuntimeError("CALENDAR_OWNER_SOURCE_MISMATCH")
    from ashare_data import BoundedCalendarView

    request = payload["query"]
    if request != {"operation": "calendar"}:
        raise RuntimeError("CALENDAR_OPERATION_DENIED")
    view = BoundedCalendarView(payload["binding"], expected_binding_sha256=pin["binding_sha256"])
    value = view.read_carry(
        "B21-qingming", "000001.XSHE", role="engine_calendar_retrospective_research"
    )
    return dict(
        query_sha256=digest(request),
        content_sha256=pin["content_sha256"],
        public_result=value,
        network_used=False,
    )


def main():
    try:
        print(
            json.dumps(execute(json.loads(sys.stdin.read())), ensure_ascii=False, allow_nan=False)
        )
    except Exception as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}))
        sys.exit(2)
