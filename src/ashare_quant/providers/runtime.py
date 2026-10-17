"""One installed data-owner candidate shared by every AQ consumer."""

import hashlib
import json
import sys
from importlib import metadata
from importlib.resources import files
from pathlib import Path

from ashare_quant.contracts import DomainError


def pin():
    return json.loads(files("ashare_quant.providers").joinpath("data-runtime.json").read_text())


def verify():
    expected = pin()
    root = Path(str(files("ashare_data")))
    actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(root.rglob("*")) if p.suffix in {".py", ".json", ".js"}}
    if metadata.version("ashare-data") != expected["version"] or actual != expected["files"]:
        raise DomainError("DATA_RUNTIME_MISMATCH", "Installed data-owner source differs from this candidate", 409)
    return expected


def installed_paths(python, source):
    """Explicit paths may only select this environment's already paired owner."""
    expected = verify()
    actual_source = Path(str(files("ashare_data"))).parent.resolve()
    selected = Path(python).expanduser().absolute()
    current = Path(sys.executable).absolute()
    if (selected.resolve() != current.resolve()
            or selected.parent.resolve() != current.parent.resolve()
            or Path(source).resolve() != actual_source):
        raise DomainError("DATA_RUNTIME_MISMATCH", "Use this environment's installed AD source and Python", 409)
    return expected
