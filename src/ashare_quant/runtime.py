"""Verify installed runtime versions; a lock-file label alone is not evidence."""

import json
import platform
from importlib import metadata
from importlib.resources import files

from .contracts import DomainError


def runtime_fingerprint(*, native=False) -> dict[str, str]:
    expected = json.loads(
        files("ashare_quant")
        .joinpath("native-runtime-lock.json" if native else "runtime-lock.json")
        .read_text()
    )
    actual = {"python": platform.python_version()}
    mismatches = []
    for name, version in expected.items():
        if name != "python":
            try:
                actual[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                actual[name] = "missing"
        if actual[name] != version:
            mismatches.append(f"{name}: expected {version}, actual {actual[name]}")
    if mismatches:
        raise DomainError("RUNTIME_DEPENDENCY_MISMATCH", "; ".join(mismatches))
    return actual
