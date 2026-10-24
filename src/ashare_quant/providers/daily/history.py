"""Pin optional owner history factors without changing the product's default data."""

import json
import os
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.core.model import digest
from ashare_data.providers.cmes_factors import CMESFactorView

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import atomic, regular, require, sha


def configured_factors():
    path = os.environ.get("ASHARE_QUANT_PYTHON_HISTORY_FACTORS")
    if not path:
        return None
    try:
        path = Path(path).expanduser().resolve()
        binding = json.loads(regular(path))
        require(set(binding) == {"root", "dataset_id"}, "PYTHON_FACTORS_CONFIGURATION")
        root = (path.parent / binding["root"]).resolve()
        view = CMESFactorView(Store(root), binding["dataset_id"])
        return {"root": str(root), "dataset_id": view.dataset_id, "description": view.describe()}
    except (OSError, ValueError, TypeError, KeyError, DataError) as exc:
        raise DomainError(
            "PYTHON_FACTORS_CONFIGURATION", "Invalid fixed history factor binding", 422
        ) from exc


def freeze_factors(binding, root):
    """The accepted job owns a verified copy; workers never reopen a mutable selector."""
    if binding is None:
        return None
    dataset_id = binding["dataset_id"]
    raw = regular(Path(binding["root"]) / "cmes_factors" / (dataset_id + ".json"))
    require(sha(raw) == dataset_id, "PYTHON_FACTORS_CHANGED")
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
    """Select immutable raw rows, then use the data owner's public price projection."""
    require(adjustment in {None, "pre", "post"}, "AQUANT_ADJUSTMENT_UNSUPPORTED")
    if adjustment is None:
        return {"rows": rows, "price_basis": "raw", "receipt": raw_receipt}
    require(
        factors is not None,
        "AQUANT_ADJUSTMENT_UNAVAILABLE",
        "Configure a fixed owner history factor snapshot, or use fq=None",
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
    return factors.adjust_prices(raw, adjustment=adjustment, basis_date=basis_date)
