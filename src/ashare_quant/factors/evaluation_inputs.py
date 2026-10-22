"""Price/input adaptation only; all evaluation mathematics belongs to AD."""

import math
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from ashare_quant.storage.artifacts import digest, require


def source_basis(spec, payload):
    if spec["kind"] == "market_release":
        return (
            "CMES pre-adjusted close; frozen factor dataset" if spec.get("cmes_factors") else None
        )
    if spec["kind"] == "alpha191":
        basis = payload.get("adjustment", "")
        return basis if basis.startswith("adjusted:") and "close" in payload["fields"] else None
    return (
        "Sina raw close × frozen effective price factor"
        if any(item.get("factors") for item in payload.get("items", []))
        else None
    )


def market_labels(raw, spec, request):
    from ashare_data.providers.cmes_factors import CMESFactorView

    binding = spec.get("cmes_factors")
    require(
        binding is not None,
        "FACTOR_EVALUATION_PRICES",
        "Register a frozen adjustment dataset for evaluation; raw factor exploration remains available",
    )
    # CMESFactorView is a read-only snapshot reader. Do not initialize an owner Store.
    factors = CMESFactorView(SimpleNamespace(root=Path(binding["root"])), binding["dataset_id"])
    end = str(request.label_end or request.end_date)
    adjusted = factors.adjust_prices(raw, adjustment="pre", basis_date=end)
    lookup = {(r["date"], r["security"]): r["close"] for r in adjusted["rows"]}
    return {
        "prices": [[lookup.get((d, s)) for s in request.securities] for d in raw["calendar"]],
        "price_basis": "CMES pre-adjusted close, basis=" + end + ", dataset=" + factors.dataset_id,
        "receipt": adjusted["receipt"],
    }


def snapshot_labels(payload, kind):
    if kind == "alpha191":
        basis = payload["adjustment"]
        require(
            basis.startswith("adjusted:") and "close" in payload["fields"],
            "FACTOR_EVALUATION_PRICES",
            "Evaluation requires a declared adjusted close panel",
        )
        return dict(prices=payload["fields"]["close"], price_basis=basis, receipt={})
    # Same existing source convention used by AD's named research factors.
    # Missing bars/factors stay missing; duplicate effective dates must agree.
    by_security = {item["security"]: item for item in payload["items"]}
    prices = []
    for day in payload["sessions"]:
        row = []
        for security in payload["securities"]:
            item = by_security[security]
            close = item.get("sina", {}).get(day, {}).get("close")
            eligible = [f for f in item.get("factors", []) if f["date"] <= day]
            value = None
            if close is not None and eligible:
                latest = max(f["date"] for f in eligible)
                factors = {Decimal(str(f["value"])) for f in eligible if f["date"] == latest}
                require(
                    len(factors) == 1,
                    "FACTOR_EVALUATION_PRICES",
                    "Conflicting effective price factors",
                )
                factor = factors.pop()
                raw_close = Decimal(str(close))
                require(
                    factor.is_finite() and factor > 0 and raw_close.is_finite() and raw_close > 0,
                    "FACTOR_EVALUATION_PRICES",
                    "Positive finite close and adjustment factor required",
                )
                value = str(raw_close * factor)
            row.append(value)
        prices.append(row)
    return dict(
        prices=prices,
        price_basis="Sina raw close × frozen effective price factor; unrounded",
        receipt={},
    )


def evaluate_frozen(values, membership, frozen, request):
    import pandas as pd
    from ashare_data.factors.evaluation import evaluate

    labels = frozen["evaluation_inputs"]
    axes = dict(index=pd.to_datetime(frozen["payload"]["sessions"]), columns=request.securities)
    prices = pd.DataFrame(labels["prices"], **axes, dtype=float)
    values = values.reindex(index=prices.index, columns=prices.columns).astype(float)
    signal_dates = prices.index[
        (prices.index >= pd.Timestamp(request.start_date))
        & (prices.index <= pd.Timestamp(request.end_date))
    ]
    values.loc[
        (values.index < pd.Timestamp(request.start_date))
        | (values.index > pd.Timestamp(request.end_date))
    ] = math.nan
    config = {
        **request.evaluation.model_dump(mode="json"),
        "input_id": digest(frozen),
        "universe_label": frozen["provenance"]["source_label"] + " / explicit fixed selection",
        "universe_mode": "fixed_sample",
        "price_basis": labels["price_basis"],
        "start_date": signal_dates[0].date().isoformat(),
        "end_date": signal_dates[-1].date().isoformat(),
    }
    return evaluate(
        values,
        prices,
        membership=membership.reindex(index=prices.index, columns=prices.columns),
        config=config,
    )
