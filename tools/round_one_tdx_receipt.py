"""Offline public AD point read; no strategy or engine admission is performed."""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def readonly(event, args):
    if event.startswith("socket.") or event in {
        "subprocess.Popen",
        "os.system",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.utime",
    }:
        raise RuntimeError("Read-only receipt refused " + event)
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        ):
            raise RuntimeError("Read-only receipt refused write")
    if event == "sqlite3.connect" and "mode=ro" not in str(args[0]):
        raise RuntimeError("Read-only receipt requires SQLite mode=ro")


def main():
    sys.addaudithook(readonly)
    path = Path(sys.argv[1]).resolve()
    binding = json.loads(path.read_text())
    if sha(binding["wheel"]["path"]) != binding["wheel"]["sha256"]:
        raise ValueError("OWNER_WHEEL_HASH_MISMATCH")
    if importlib.metadata.version("ashare-data") != binding["ad_version"]:
        raise ValueError("OWNER_VERSION_MISMATCH")
    site = Path(importlib.util.find_spec("ashare_data").origin).parent.parent
    with zipfile.ZipFile(binding["wheel"]["path"]) as wheel:
        inventory = {}
        for name in wheel.namelist():
            if name.startswith("ashare_data/") and not name.endswith("/"):
                if (site / name).read_bytes() != wheel.read(name):
                    raise ValueError("OWNER_INSTALLED_FILE_MISMATCH:" + name)
                inventory[name] = sha(site / name)
    from ashare_data import DataError, Store

    view = Store(binding["owner_store"]).tdx_points(binding["dataset_id"])
    requests = [
        ("2024-03-04T13:01:00+08:00", "retrospective_point_research"),
        ("2024-03-04T15:00:00+08:00", "retrospective_point_research"),
        ("2024-03-04T13:00:00+08:00", "retrospective_point_research"),
        ("2024-03-04T13:01:00+08:00", "strategy"),
        ("2024-03-04T13:01:00+08:00", "matching"),
    ]
    results = []
    for at, role in requests:
        request = {"security": "000001.XSHE", "at": at, "role": role}
        try:
            value = view.read(**request)
            results.append({"request": request, "result": value, "error": None})
        except DataError as exc:
            results.append(
                {
                    "request": request,
                    "result": None,
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
    print(
        json.dumps(
            {
                "schema": "aq.round-one.tdx-public-receipt.v1",
                "binding": str(path),
                "binding_sha256": sha(path),
                "ad_version": binding["ad_version"],
                "dataset_id": binding["dataset_id"],
                "wheel": binding["wheel"],
                "installed_owner_files_verified": len(inventory),
                "installed_owner_files": inventory,
                "reads": results,
                "network_used": False,
                "strategy_or_matching_admitted": False,
                "engine_run": False,
                "historical_pit": False,
                "decision": "POINT_RESEARCH_READ_ONLY; NOT_ADMITTED_TO_EXISTING_NATIVE_BAR_CONTRACT",
                "missing": binding["missing"],
                "reason": "No OHLC synthesis or JQ current quote equivalence. A separate explicit point-price execution model has not been implemented or validated.",
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
