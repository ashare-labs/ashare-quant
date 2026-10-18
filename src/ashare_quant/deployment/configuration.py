"""Path resolution never creates directories or silently selects example data."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ashare_quant.contracts import DomainError

SCHEMA = "ashare-deployment.v1"


@dataclass(frozen=True)
class Asset:
    name: str
    kind: str
    path: Path
    role: str
    retention: str


@dataclass(frozen=True)
class Configuration:
    deployment: Path
    profile: str
    kind: str
    data_root: Path
    control_db: Path
    paper_root: Path
    daily_root: Path
    release_store_root: Path
    owner_store_root: Path
    api_port: int
    paper_port: int
    web_port: int
    default_dataset_id: str | None
    assets: tuple[Asset, ...]
    selected_assets: tuple[str, ...]
    document: dict

    def asset(self, name):
        return next(item for item in self.assets if item.name == name)


def _path(value, base):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Paths must be nonempty strings")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _port(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("Ports must be integers between 1024 and 65535")
    return value


def resolve(deployment=None, profile="product") -> Configuration:
    """Explicit path > ASHARE_DEPLOYMENT > ASHARE_DATA_ROOT/deployment.json."""
    value = deployment or os.environ.get("ASHARE_DEPLOYMENT")
    if not value and os.environ.get("ASHARE_DATA_ROOT"):
        value = Path(os.environ["ASHARE_DATA_ROOT"]).expanduser() / "deployment.json"
    if not value:
        raise DomainError(
            "DEPLOYMENT_NOT_CONFIGURED",
            "Select --deployment or set ASHARE_DEPLOYMENT / ASHARE_DATA_ROOT; no sample fallback",
            503,
        )
    try:
        path = Path(value).expanduser().resolve(strict=True)
        document = json.loads(path.read_text())
        if document["schema"] != SCHEMA:
            raise ValueError("Unsupported deployment schema")
        root = _path(document["data_root"], path.parent)
        assets = tuple(
            Asset(name, item["kind"], _path(item["path"], root), item["role"],
                  item.get("retention", "preserve"))
            for name, item in document["assets"].items()
        )
        by_name = {item.name: item for item in assets}
        if not assets or any(
            not isinstance(v, str) or not v for a in assets
            for v in (a.name, a.kind, a.role, a.retention)
        ):
            raise ValueError("Assets require names, kind, role and retention")
        entry = document["profiles"][profile]
        if entry["kind"] not in {"product", "evaluation", "research"}:
            raise ValueError("Profile kind must be product, evaluation or research")
        daily = by_name[entry["daily_asset"]]
        release = by_name[entry["release_store_asset"]]
        owner = by_name[entry.get("owner_store_asset", entry["release_store_asset"])]
        selected = tuple(dict.fromkeys([
            daily.name, release.name, owner.name, *entry.get("required_assets", [])
        ]))
        for name in selected:
            by_name[name]
        if daily.kind != "local_daily" or release.kind != "store" or owner.kind != "store":
            raise ValueError("daily_asset must be local_daily; release/owner assets must be Store")
        control = _path(entry["control_db"], root)
        paper = _path(entry["paper_root"], root)
        # Runtime writers must never be placed inside the original source asset.
        for state in (control, paper, release.path):
            if state == daily.path or state.is_relative_to(daily.path):
                raise ValueError("Runtime state and publications must be outside the main source asset")
        if control == paper / "control.sqlite3":
            raise ValueError("AQ control database and paper database must be distinct")
        ports = tuple(_port(entry.get(key, default)) for key, default in (
            ("api_port", 18839), ("paper_port", 18818), ("web_port", 41839)
        ))
        if len(set(ports)) != 3:
            raise ValueError("API, paper and web require distinct ports")
        default_dataset = entry.get("default_dataset_id")
        if default_dataset is not None and (
            not isinstance(default_dataset, str) or not re.fullmatch(r"[0-9a-f]{64}", default_dataset)
        ):
            raise ValueError("default_dataset_id must be a lowercase SHA256 or null")
        return Configuration(
            path, profile, entry["kind"], root, control, paper, daily.path, release.path,
            owner.path, *ports, default_dataset, assets, selected, document,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DomainError("DEPLOYMENT_INVALID", f"Invalid deployment: {exc}", 503) from exc
