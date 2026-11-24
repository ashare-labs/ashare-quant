"""Pin verified owner history factors from deployment.json or environment; never guess factors."""

import json
import os
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.core.model import digest
from ashare_data.providers.cmes import CMESFactorView

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import atomic, regular, require


def _load_factor_binding(root, dataset_id):
    try:
        resolved = Path(root).expanduser().resolve()
        view = CMESFactorView(Store(resolved), dataset_id)
        return {
            "root": str(resolved),
            "dataset_id": view.dataset_id,
            "description": view.describe(),
        }
    except (OSError, ValueError, TypeError, KeyError, DataError) as exc:
        raise DomainError(
            "PYTHON_FACTORS_CONFIGURATION", "Invalid fixed history factor binding", 422
        ) from exc


def deployment_factors(config):
    """Resolve optional cmes_factors configured on a deployment profile."""
    if config is None:
        return None
    spec = getattr(config, "cmes_factors", None)
    if spec is None and hasattr(config, "document") and hasattr(config, "profile"):
        entry = config.document.get("profiles", {}).get(config.profile, {})
        raw = entry.get("cmes_factors")
        if raw is not None:
            try:
                if "asset" in raw and hasattr(config, "asset"):
                    root = config.asset(raw["asset"]).path
                else:
                    base = getattr(config, "data_root", Path("."))
                    cand = Path(raw["root"]).expanduser()
                    root = (cand if cand.is_absolute() else Path(base) / cand).resolve()
                spec = {"root": str(root), "dataset_id": raw["dataset_id"]}
            except (KeyError, TypeError, AttributeError, OSError) as exc:
                raise DomainError(
                    "PYTHON_FACTORS_CONFIGURATION", "Invalid deployment cmes_factors", 422
                ) from exc
    if spec is None:
        return None
    return _load_factor_binding(spec["root"], spec["dataset_id"])


def configured_factors(product=None):
    path = os.environ.get("ASHARE_QUANT_PYTHON_HISTORY_FACTORS")
    if path:
        try:
            resolved_path = Path(path).expanduser().resolve()
            binding = json.loads(regular(resolved_path))
            require(set(binding) == {"root", "dataset_id"}, "PYTHON_FACTORS_CONFIGURATION")
            root = (resolved_path.parent / binding["root"]).resolve()
        except (OSError, ValueError, TypeError, KeyError, DomainError) as exc:
            raise DomainError(
                "PYTHON_FACTORS_CONFIGURATION", "Invalid fixed history factor binding", 422
            ) from exc
        return _load_factor_binding(root, binding["dataset_id"])
    if product is not None:
        try:
            config = product.configured()
        except DomainError:
            return None
        return deployment_factors(config)
    return None


def freeze_factors(binding, root):
    """The accepted job owns a verified copy; workers never reopen a mutable selector."""
    if binding is None:
        return None
    dataset_id = binding["dataset_id"]
    raw = regular(Path(binding["root"]) / "cmes_factors" / (dataset_id + ".json"))
    store = Store.init(root / "history-factors")
    directory = store.root / "cmes_factors"
    directory.mkdir()
    atomic(directory / (dataset_id + ".json"), raw)
    view = CMESFactorView(store, dataset_id)
    return {"dataset_id": dataset_id, "description": view.describe()}


def frozen_factors(root, binding):
    return (
        None
        if binding is None
        else CMESFactorView(Store(root / "history-factors"), binding["dataset_id"])
    )


def history_prices(rows, raw_receipt, factors, adjustment, basis_date):
    """Select immutable raw rows, then use the data owner's verified factor projection."""
    require(adjustment in {None, "pre", "post"}, "AQUANT_ADJUSTMENT_UNSUPPORTED")
    if adjustment is None:
        return {"rows": rows, "price_basis": "raw", "receipt": raw_receipt}
    require(
        factors is not None,
        "AQUANT_ADJUSTMENT_UNAVAILABLE",
        "Configure cmes_factors in deployment.json (or ASHARE_QUANT_PYTHON_HISTORY_FACTORS), or use fq=None",
    )
    raw = {
        "price_basis": "raw",
        "rows": rows,
        "end": max(r["date"] for r in rows),
        "receipt": {
            "schema": "aq-visible-daily-selection.v1",
            "raw_receipt": raw_receipt,
            "rows_sha256": digest(rows),
        },
    }
    try:
        return factors.adjust_prices(raw, adjustment=adjustment, basis_date=basis_date)
    except DataError as exc:
        raise DomainError(
            exc.code, exc.message, 422, details=exc.details, origin="ashare-data"
        ) from exc
