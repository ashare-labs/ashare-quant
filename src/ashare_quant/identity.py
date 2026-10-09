"""Identify the exact installed backend sources, including editable checkouts."""

import hashlib
import json
from importlib.util import find_spec
from pathlib import Path

from .contracts import DomainError


def source_inventory() -> dict[str, str]:
    """Both shipped packages, including nested source, fixtures and identity inputs."""
    result = {}
    try:
        for package in ("ashare_quant", "rqprobe"):
            root = Path(next(iter(find_spec(package).submodule_search_locations)))
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix in {".py", ".json", ".sha256"}:
                    result[f"{package}/{path.relative_to(root).as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ImportError, AttributeError, TypeError, StopIteration) as exc:
        raise DomainError("BACKEND_CHANGED", "Installed implementation inventory is unavailable") from exc
    return result


def source_sha256() -> str:
    return hashlib.sha256(json.dumps(source_inventory(), sort_keys=True).encode()).hexdigest()
