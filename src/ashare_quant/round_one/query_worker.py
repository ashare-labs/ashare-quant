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
    pin = json.loads(Path(__file__).with_name("query-owner-pin.json").read_text())
    for name, expected in pin["runtime"].items():
        actual = platform.python_version() if name == "python" else importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError("QUERY_OWNER_RUNTIME_MISMATCH:" + name)
    root = Path(importlib.util.find_spec("ashare_data").origin).parent
    inventory = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".json", ".sha256"}
    }
    if inventory != pin["files"] or digest(inventory) != pin["content_sha256"]:
        raise RuntimeError("QUERY_OWNER_SOURCE_MISMATCH")
    from ashare_data import DataError, Store

    view = Store(payload["store"]).jq_queries(payload["dataset_id"])
    request = payload["query"]
    try:
        if request["operation"] == "describe":
            result = view.describe()
        elif request["operation"] == "query":
            result = view.query(**request["parameters"]).to_dict()
        else:
            raise RuntimeError("QUERY_OWNER_OPERATION_DENIED")
        error = None
    except DataError as exc:
        result = None
        error = {
            "origin": "ashare-data",
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
    return {
        "query_sha256": digest(request),
        "content_sha256": pin["content_sha256"],
        "dataset_id": payload["dataset_id"],
        "public_result": result,
        "error": error,
        "network_used": False,
    }


def main():
    try:
        print(
            json.dumps(execute(json.loads(sys.stdin.read())), ensure_ascii=False, allow_nan=False)
        )
    except Exception as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}))
        sys.exit(2)
