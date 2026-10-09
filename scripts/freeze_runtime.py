"""Build-time tool: freeze the active runtime dependency closure against uv.lock."""

import hashlib
import json
import platform
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1]
lock = tomllib.loads((root / "uv.lock").read_text())
versions = {p["name"]: p["version"] for p in lock["package"]}
project = tomllib.loads((root / "pyproject.toml").read_text())
pending = list(project["project"]["dependencies"])
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
(target / "runtime-lock.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
(target / "lock.sha256").write_text(hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest() + "\n")
print(f"Verified and froze {len(result) - 1} runtime distributions")
