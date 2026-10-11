"""Pinned public TDX point reader; no raw payload decoding or market synthesis."""

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
    pin = json.loads(Path(__file__).with_name("point-owner-pin.json").read_text())
    for name, expected in pin["runtime"].items():
        value = platform.python_version() if name == "python" else importlib.metadata.version(name)
        if value != expected:
            raise RuntimeError("POINT_OWNER_RUNTIME_MISMATCH:" + name)
    root = Path(importlib.util.find_spec("ashare_data").origin).parent
    inventory = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".json", ".sha256"}
    }
    if inventory != pin["files"] or digest(inventory) != pin["content_sha256"]:
        raise RuntimeError("POINT_OWNER_SOURCE_MISMATCH")
    from ashare_data import Store

    q = payload["query"]
    if q["operation"] != "read":
        raise RuntimeError("POINT_OWNER_OPERATION_DENIED")
    result = Store(payload["store"]).tdx_points(payload["dataset_id"]).read(**q["parameters"])
    return dict(
        query_sha256=digest(q),
        content_sha256=pin["content_sha256"],
        dataset_id=payload["dataset_id"],
        public_result=result,
        network_used=False,
    )


def main():
    from ashare_quant.execution_six.owner_worker import main as worker_main

    worker_main(execute)
