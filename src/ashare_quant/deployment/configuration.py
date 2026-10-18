"""Path resolution never creates directories or silently selects example data."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ashare_quant.contracts import DomainError

SCHEMA = "ashare-deployment.v1"
REFERENCE_KINDS = {"daily_research", "daily_execution", "historical_market", "corporate_history"}


@dataclass(frozen=True)
class Asset:
    name: str
    kind: str
    path: Path
    role: str
    retention: str
    binding: str | None = None
    binding_sha256: str | None = None


@dataclass(frozen=True)
class Binding:
    asset_name: str
    kind: str
    path: Path
    binding_path: Path
    binding_sha256: str

    def reference(self):
        return {"binding_path": str(self.binding_path), "binding_sha256": self.binding_sha256}


@dataclass(frozen=True)
class ReferenceSet:
    name: str
    kind: str
    corporate: Binding
    research: Binding | None = None
    execution: Binding | None = None
    market: Binding | None = None

    def references(self):
        return {"name": self.name, "kind": self.kind, **{
            role: binding.reference() for role in ("research", "execution", "market", "corporate")
            if (binding := getattr(self, role)) is not None
        }}

    def source_bindings(self):
        return {
            role: {"kind": binding.kind, "binding_sha256": binding.binding_sha256}
            for role in ("research", "execution", "market", "corporate")
            if (binding := getattr(self, role)) is not None
        }


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
    reference_sets: tuple[ReferenceSet, ...] = ()
    default_reference_set: str | None = None

    def asset(self, name):
        return next(item for item in self.assets if item.name == name)

    def references(self, name=None):
        selected = name if name is not None else self.default_reference_set
        if selected is None:
            return None
        for reference in self.reference_sets:
            if reference.name == selected:
                return reference.references()
        raise DomainError(
            "PRODUCT_REFERENCE_SET_UNKNOWN", "Select a configured reference set", 422,
            details={"reference_set": selected},
        )


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("binding_sha256 must be a lowercase SHA256")
    return value


def _asset(name, item, root):
    kind, path = item["kind"], _path(item["path"], root)
    binding, pin = None, None
    if kind in REFERENCE_KINDS:
        binding = item.get("binding", "binding.json")
        relative = Path(binding)
        if (not isinstance(binding, str) or not binding or "\x00" in binding
                or relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != binding or binding == "."
                or not (path / relative).resolve().is_relative_to(path)):
            raise ValueError("Binding must be a relative file inside its asset package")
        pin = _sha(item["binding_sha256"])
    return Asset(name, kind, path, item["role"], item.get("retention", "preserve"), binding, pin)


def _references(entry, by_name):
    declarations = entry.get("reference_sets", {})
    if not isinstance(declarations, dict):
        raise ValueError("reference_sets must be a named object")

    def binding(name, kind):
        asset = by_name[name]
        role = "corporate_source" if kind == "corporate_history" else "market_reference"
        if asset.kind != kind or asset.role != role:
            raise ValueError(f"Reference asset {name} must have kind {kind} and role {role}")
        return Binding(name, kind, asset.path, asset.path / asset.binding, asset.binding_sha256)

    result = []
    for name, item in declarations.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Reference set names must be nonempty strings up to 80 characters")
        kind = item["kind"]
        corporate = binding(item["corporate_history_asset"], "corporate_history")
        if kind == "daily_execution":
            if set(item) != {"kind", "corporate_history_asset", "research_asset", "execution_asset"}:
                raise ValueError("daily_execution reference set needs research and execution assets")
            result.append(ReferenceSet(name, kind, corporate,
                research=binding(item["research_asset"], "daily_research"),
                execution=binding(item["execution_asset"], "daily_execution")))
        elif kind == "historical_market":
            if set(item) != {"kind", "corporate_history_asset", "market_asset"}:
                raise ValueError("historical_market reference set needs a market asset")
            result.append(ReferenceSet(name, kind, corporate,
                                       market=binding(item["market_asset"], "historical_market")))
        else:
            raise ValueError("Unknown reference set kind")
    default = entry.get("default_reference_set")
    if declarations and (not isinstance(default, str) or default not in declarations):
        raise ValueError("Choose an explicit configured default_reference_set")
    if not declarations and default is not None:
        raise ValueError("default_reference_set has no corresponding reference set")
    return tuple(result), default


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
        assets = tuple(_asset(name, item, root) for name, item in document["assets"].items())
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
        references, default_reference = _references(entry, by_name)
        reference_assets = [getattr(ref, role).asset_name for ref in references
                            for role in ("research", "execution", "market", "corporate")
                            if getattr(ref, role) is not None]
        selected = tuple(dict.fromkeys([
            daily.name, release.name, owner.name, *reference_assets, *entry.get("required_assets", [])
        ]))
        for name in selected:
            by_name[name]
        if daily.kind != "local_daily" or release.kind != "store" or owner.kind != "store":
            raise ValueError("daily_asset must be local_daily; release/owner assets must be Store")
        control = _path(entry["control_db"], root)
        paper = _path(entry["paper_root"], root)
        # Runtime writers must never be placed inside the original source asset.
        for state in (control, paper, release.path):
            for source in (daily.path, *(by_name[name].path for name in reference_assets)):
                if state == source or state.is_relative_to(source):
                    raise ValueError("Runtime state and publications must be outside source assets")
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
            references, default_reference,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DomainError("DEPLOYMENT_INVALID", f"Invalid deployment: {exc}", 503) from exc
