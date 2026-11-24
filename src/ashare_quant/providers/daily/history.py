"""Pin optional owner history factors or derive adjustments from frozen release bars."""

import json
import os
from bisect import bisect_right
from copy import deepcopy
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.core.model import digest
from ashare_data.providers.cmes import CMESFactorView

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import atomic, regular, require

PRICE_FIELDS = ("open", "high", "low", "close")
DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


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


def _positive_decimal(value):
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def cumulative_factors_from_bars(bars):
    """Derive cumulative hfq factors from chronological observed bars (close_{t-1} / preclose_t)."""
    dates, factors = [], []
    hfq = Decimal(1)
    prev_close = None
    with localcontext(DECIMAL_CONTEXT):
        for row in bars:
            if row is None or row.get("assumption"):
                continue
            cl = _positive_decimal(row.get("close"))
            pre = _positive_decimal(row.get("preclose"))
            if prev_close is not None and pre is not None and pre != prev_close:
                hfq = hfq * (prev_close / pre)
            dates.append(row["date"])
            factors.append(hfq)
            if cl is not None:
                prev_close = cl
    return dates, factors


def release_adjust_prices(rows, raw_receipt, series_by_security, adjustment, basis_date):
    """Adjust prices from release preclose ratios when no external CMES snapshot is pinned."""
    require(adjustment in {None, "pre", "post"}, "AQUANT_ADJUSTMENT_UNSUPPORTED")
    if adjustment is None:
        return {"rows": rows, "price_basis": "raw", "receipt": raw_receipt}
    result_rows = deepcopy(rows)
    anchors = {}
    with localcontext(DECIMAL_CONTEXT):
        curves = {}
        for sid in {r["security"] for r in result_rows}:
            dates, factors = cumulative_factors_from_bars(series_by_security.get(sid, ()))
            curves[sid] = (dates, factors)
            if not dates:
                anchors[sid] = (basis_date, Decimal(1))
            elif adjustment == "pre":
                idx = bisect_right(dates, basis_date) - 1
                anchor_idx = max(0, idx)
                anchors[sid] = (dates[anchor_idx], factors[anchor_idx])
            else:
                anchors[sid] = (dates[0], factors[0])
        for row in result_rows:
            sid, d = row["security"], row["date"]
            dates, factors = curves[sid]
            if dates:
                idx = max(0, bisect_right(dates, d) - 1)
                event_date, factor = dates[idx], factors[idx]
            else:
                event_date, factor = d, Decimal(1)
            _, anchor_factor = anchors[sid]
            ratio = factor / anchor_factor
            for field in PRICE_FIELDS:
                val = _positive_decimal(row.get(field))
                if val is not None:
                    row[field] = str(val * ratio)
            row.update(adjustment_factor=str(ratio), factor_effective_date=event_date)
    description = {
        "source": "market_release_preclose",
        "mode": adjustment,
        "basis_date": basis_date,
        "price_anchor_dates": {
            s: (basis_date if adjustment == "pre" else anchor_date)
            for s, (anchor_date, _) in anchors.items()
        },
        "adjusted_fields": list(PRICE_FIELDS),
        "historical_pit": False,
        "execution_permission": False,
    }
    receipt = {
        "schema": "aq-release-adjusted-daily-read.v1",
        "raw_receipt": raw_receipt,
        "adjustment": description,
        "network_used": False,
    }
    return {
        "rows": result_rows,
        "price_basis": adjustment,
        "adjustment": description,
        "receipt": {**receipt, "receipt_sha256": digest(receipt)},
    }


def history_prices(rows, raw_receipt, factors, adjustment, basis_date, *, series_by_security=None):
    """Select immutable raw rows, then use pinned CMES factors or release preclose ratios."""
    require(adjustment in {None, "pre", "post"}, "AQUANT_ADJUSTMENT_UNSUPPORTED")
    if adjustment is None:
        return {"rows": rows, "price_basis": "raw", "receipt": raw_receipt}
    if factors is None and series_by_security is not None:
        return release_adjust_prices(rows, raw_receipt, series_by_security, adjustment, basis_date)
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
