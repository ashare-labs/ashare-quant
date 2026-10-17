"""Standalone fixed-owner reader run with an isolated Python; never import AQ engines."""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def readonly(event, args):
    # Fail closed for accidental writes/network in this trusted reader, not a malicious-code sandbox.
    if event.startswith("socket.") or event in {
        "subprocess.Popen", "os.system", "os.mkdir", "os.remove", "os.rename", "os.rmdir",
        "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown", "os.utime",
    }:
        raise RuntimeError("Read-only owner reader refused " + event)
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise RuntimeError("Read-only owner reader refused file write")
    if event == "sqlite3.connect" and "mode=ro" not in str(args[0]):
        raise RuntimeError("Read-only owner reader requires SQLite mode=ro")


def execute(payload):
    sys.addaudithook(readonly)
    pin = payload["pin"]
    if platform.python_version() != pin["runtime"]["python"]:
        raise ValueError("Research Python version differs from the fixed pin")
    for name, version in pin["runtime"].items():
        if name != "python" and importlib.metadata.version(name) != version:
            raise ValueError("Research dependency differs: " + name)
    spec = importlib.util.find_spec("ashare_data")
    root = Path(spec.origin).parent
    inventory = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".py", ".json", ".js"}}
    if inventory != pin["files"] or digest(inventory) != pin["content_sha256"]:
        raise ValueError("Research owner package content differs from the fixed wheel")
    from ashare_data import DataError, Store

    def call(operation):
        try:
            return {"status": "readable", "public_result": operation().to_dict(), "error": None}
        except DataError as exc:
            return {"status": "refused", "public_result": None,
                    "error": {"origin": "ashare-data", "code": exc.code, "message": exc.message, "details": exc.details}}

    def view():
        return Store(payload["store"]).baostock(payload["capture_id"])

    # Only documented public methods. No row parsing, private facts or local calendar inference.
    calendar = call(lambda: view().get_calendar(require_known=False))
    links = call(lambda: view().get_calendar_links(trading_dates=payload["query"]["trading_dates"]))
    query = payload["query"]
    inputs = {"security": query["security"]}
    inputs.update({name: query[name] for name in ("price", "status", "preclose") if query[name] is not None})
    preflight_request = {"inputs": [inputs], "calendar": {"kind": "baostock", "version_id": payload["capture_id"]},
                         "trading_dates": query["trading_dates"]}
    preflight = call(lambda: Store(payload["store"]).preflight_research_request(
        preflight_request, require_complete=query["require_complete"]))
    return {"protocol": "aq.owner-market-reader.v1", "query_sha256": digest(payload["query"]),
            "capture_id": payload["capture_id"], "content_sha256": pin["content_sha256"],
            "calendar": calendar, "calendar_links": links, "research_preflight": preflight, "network_used": False}


if __name__ == "__main__":
    try:
        value = execute(json.loads(sys.stdin.read()))
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": {"code": "DIAGNOSTIC_READER_FAILED", "message": str(exc), "type": type(exc).__name__}}))
        sys.exit(2)
