"""Read one public owner request in its separately pinned, read-only runtime."""

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path

from ashare_quant.diagnostics.runner import digest, readonly


def execute(payload, *, pin_path=None):
    sys.addaudithook(readonly)
    pin = json.loads((pin_path or Path(__file__).with_name("owner-pin.json")).read_text())
    for name, expected in pin["runtime"].items():
        actual = platform.python_version() if name == "python" else importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError("SIX_OWNER_RUNTIME_MISMATCH:" + name)
    root = Path(importlib.util.find_spec("ashare_data").origin).parent
    inventory = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob("*"))
                 if p.is_file() and p.suffix in {".py", ".json", ".sha256"}}
    if inventory != pin["files"] or digest(inventory) != pin["content_sha256"]:
        raise RuntimeError("SIX_OWNER_SOURCE_MISMATCH")
    from ashare_data import Store
    view = Store(payload["store"]).jq_observations(payload["dataset_id"])
    query = payload["query"]
    if query["operation"] == "describe":
        result = view.describe()
    elif query["operation"] == "bootstrap":
        result = view.bootstrap(query["security"], query["day"])
    elif query["operation"] == "read":
        result = view.read(**query["parameters"]).to_dict()
    else:
        raise RuntimeError("SIX_OWNER_OPERATION_DENIED")
    return {"query_sha256": digest(query), "content_sha256": pin["content_sha256"],
            "dataset_id": payload["dataset_id"], "public_result": result, "network_used": False}


def main(executor=execute):
    try:
        print(json.dumps(executor(json.loads(sys.stdin.read())), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}))
        sys.exit(2)
