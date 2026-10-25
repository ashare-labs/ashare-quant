"""Verify owner releases, then preserve AQ's immutable account-model projection."""

import json
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.portfolio.corporate_export import attach
from ashare_quant.portfolio.inputs import digest as input_digest
from ashare_quant.portfolio.product_reference import owner_digest, source_bindings, validate
from ashare_quant.storage.artifacts import atomic, canonical, lock, sha


def require(condition, detail):
    if not condition:
        raise DomainError("PRODUCT_OWNER_CONTRACT_MISMATCH",
                          "Published source identity differs: " + detail, 409)


def scope(selection):
    return {"securities": sorted(selection.securities), "start": str(selection.start),
            "end": str(selection.end), "source": selection.source}


def reference_identity(references):
    return {k: references[k] for k in ("name", "kind")} if references else None


def check_coverage(value, selection, references):
    try:
        require(value["schema"] == "ad-local-daily-coverage.v1", "coverage schema")
        require(value["scope"] == scope(selection), "coverage selection")
        require(value["reference_set"] == reference_identity(references), "coverage reference set")
        require(value["source_bindings"] == source_bindings(references), "coverage bindings")
        require(type(value["ready"]) is bool and isinstance(value["gaps"], list)
                and value["ready"] == (not value["gaps"]), "coverage readiness")
        require(not value["ready"] or (references is not None and value["owner_api_reads"]),
                "coverage source reads")
        require(owner_digest({k: v for k, v in value.items() if k != "coverage_sha256"})
                == value["coverage_sha256"], "coverage digest")
    except (ValueError, KeyError, TypeError) as exc:
        raise DomainError("PRODUCT_OWNER_CONTRACT_MISMATCH",
                          "Malformed source coverage response", 409) from exc


def _read(root, name):
    require(name in {"input.json", "manifest.json", "corporate-schedule.json", "coverage.json"},
            "release file name")
    path = root / name
    require(not path.is_symlink() and path.is_file(),
            "release file missing or linked")
    raw = path.read_bytes()
    return raw, json.loads(raw)


def _file(root, manifest, prefix, name, hash_field=None):
    require(manifest[prefix + "_path"] == name, prefix + " path")
    raw, value = _read(root, name)
    require(sha(raw) == manifest[hash_field or prefix + "_sha256"]
            and len(raw) == manifest[prefix + "_bytes"], prefix + " hash/bytes")
    return raw, value


def consume(release, selection, config, references):
    """No source mutation. Public Store validation precedes AQ's independent binding checks."""
    try:
        return _consume(release, selection, config, references)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DomainError("PRODUCT_OWNER_CONTRACT_MISMATCH",
                          "Published input or reference evidence is malformed or unavailable", 409,
                          details={"failure": type(exc).__name__, "detail": str(exc)[:400]}) from exc


def _consume(release, selection, config, references):
    from ashare_data import Store

    store = Store(config.release_store_root)
    view = store.local_daily_product(release["dataset_id"])
    public_manifest, public_input = view.describe(), view.read()
    root = config.release_store_root / "local_daily_products" / release["dataset_id"]
    require(root.resolve().is_relative_to(config.release_store_root.resolve())
            and not root.is_symlink() and not root.parent.is_symlink(), "release directory")
    manifest_raw, manifest = _read(root, "manifest.json")
    require(manifest == release["manifest"] == public_manifest
            and owner_digest(manifest) == release["dataset_id"], "manifest identity")
    requested = scope(selection)
    require(manifest["securities"] == requested["securities"]
            and manifest["start"] == requested["start"] and manifest["end"] == requested["end"]
            and manifest["source_selection"] == selection.source, "release selection")
    require(manifest["mode"] == release["mode"] == selection.mode, "release mode")
    base_raw, base = _file(root, manifest, "portfolio_input", "input.json")
    require(base == public_input, "public input read")
    require(base["schema"] == "ad-daily-portfolio.v1" and base["real_market_data"] is True
            and base["producer"]["name"] == "ashare-data"
            and base["price_basis"] == "raw" and base["volume_unit"] == "share",
            "real raw input contract")
    require(Path(release["portfolio_input_path"]) == root / "input.json"
            == view.portfolio_input_path, "input path")
    require(release["portfolio_input_sha256"] == sha(base_raw), "input response hash")
    captured = {"base-input.json": base_raw, "ad-manifest.json": manifest_raw}
    if selection.mode == "price_only_no_limits":
        require(base["research_model"] == "price_only_no_actions"
                and base["price_limit_policy"] == "disabled_for_price_only_diagnostic"
                and "corporate_schedule" not in base and not base.get("reference_evidence"),
                "diagnostic scope")
        # AD's diagnostic artifact is already a complete consumer input.
        return root / "input.json"
    require(references is not None, "ordinary reference configuration")
    bindings, identity = source_bindings(references), reference_identity(references)
    require(manifest["source_bindings"] == release["source_bindings"] == bindings,
            "release bindings")
    require(manifest["reference_set"] == release["reference_set"] == identity,
            "release reference set")
    schedule_raw, schedule = _file(root, manifest, "corporate_schedule", "corporate-schedule.json")
    coverage_raw, coverage = _file(root, manifest, "coverage", "coverage.json", "coverage_file_sha256")
    require(schedule == view.read_corporate_schedule() and coverage == view.read_coverage(),
            "public source reads")
    require(Path(release["corporate_schedule_path"]) == root / "corporate-schedule.json"
            and release["corporate_schedule_sha256"] == sha(schedule_raw), "schedule response")
    check_coverage(coverage, selection, references)
    require(coverage["ready"] is True and release["coverage_sha256"]
            == manifest["coverage_sha256"] == coverage["coverage_sha256"], "published coverage")
    require(base["reference_evidence"]["source_bindings"] == bindings
            and base["reference_evidence"]["reference_set"] == identity
            and base["reference_evidence"]["scope"]
            == {k: requested[k] for k in ("securities", "start", "end")}, "input reference scope")
    document = attach(base, schedule, accept_date_model=True)
    document["product_release"] = {
        "schema": "aq-product-release.v1", "dataset_id": release["dataset_id"],
        "mode": selection.mode, "manifest": manifest, "manifest_file_sha256": sha(manifest_raw),
        "portfolio_input_sha256": sha(base_raw), "portfolio_input_semantic_sha256": input_digest(base),
        "corporate_schedule_sha256": sha(schedule_raw),
        "corporate_schedule_semantic_sha256": input_digest(schedule),
    }
    document["product_coverage"] = coverage
    document["source_refs"].append({
        "kind": "ad_product_release", "dataset_id": release["dataset_id"],
        "mode": selection.mode, "reference_set": identity, "source_bindings": bindings,
        "portfolio_input_sha256": sha(base_raw), "corporate_schedule_sha256": sha(schedule_raw),
        "coverage_sha256": coverage["coverage_sha256"],
    })
    validate(document)
    raw = canonical(document)
    captured.update({"corporate-schedule.json": schedule_raw, "coverage.json": coverage_raw,
                     "input.json": raw})
    inputs = config.control_db.parent / "inputs"
    require(not inputs.is_symlink(), "consumer inputs directory")
    with lock(inputs / "publication.lock", timeout=10):
        destination = inputs / sha(raw)
        require(not destination.is_symlink(), "consumer content directory")
        destination.mkdir(exist_ok=True, mode=0o700)
        for name, content in captured.items():
            path = destination / name
            require(not path.is_symlink(), "consumer artifact link")
            if path.exists():
                require(path.read_bytes() == content, "existing consumer artifact differs")
            else:
                atomic(path, content)
    return destination / "input.json"
