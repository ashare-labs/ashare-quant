"""Build-time tool: freeze the active runtime dependency closure against uv.lock."""

import argparse
import hashlib
import json
import platform
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true")
parser.add_argument("--check", action="store_true", help="Fail on stale snapshots without writing files")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
lock = tomllib.loads((root / "uv.lock").read_text())
versions = {p["name"]: p["version"] for p in lock["package"]}
project = tomllib.loads((root / "pyproject.toml").read_text())
pending = list(project["project"]["dependencies"])
if args.native:
    pending.extend(project["project"]["optional-dependencies"]["native"])
result = {"python": platform.python_version()}
while pending:
    req = Requirement(pending.pop())
    if req.marker and not req.marker.evaluate({"extra": ""}):
        continue
    name = req.name.lower().replace("_", "-")
    if name in result:
        continue
    version = metadata.version(name)
    assert version == versions[name], (name, version, versions[name])
    result[name] = version
    pending.extend(metadata.requires(name) or [])
target = root / "src/ashare_quant"
outputs = {
    target / ("native-runtime-lock.json" if args.native else "runtime-lock.json"):
        json.dumps(result, sort_keys=True, indent=2) + "\n",
    target / "lock.sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest() + "\n",
}
if args.check:
    stale = [path.name for path, text in outputs.items()
             if not path.is_file() or path.read_text() != text]
    if stale:
        parser.exit(1, "Stale dependency snapshots: " + ", ".join(stale) + "\n")
    print(f"Verified {len(result) - 1} runtime distributions; snapshots are current")
else:
    for path, text in outputs.items():
        path.write_text(text)
    print(f"Verified and froze {len(result) - 1} runtime distributions")
