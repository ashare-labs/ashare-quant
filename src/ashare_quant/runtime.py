"""Verify installed runtime versions; a lock-file label alone is not evidence."""

import json
import platform
from importlib import metadata
from importlib.resources import files

from ashare_quant.contracts import DomainError


def runtime_fingerprint(*, native=False) -> dict[str, str]:
    expected = json.loads(
        files("ashare_quant")
        .joinpath("native-runtime-lock.json" if native else "runtime-lock.json")
        .read_text()
    )
    actual = {"python": platform.python_version()}
    for name, version in expected.items():
        if name != "python":
            try:
                actual[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                actual[name] = version
    return actual
