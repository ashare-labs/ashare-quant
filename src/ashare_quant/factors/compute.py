"""Call existing AD definitions and preserve per-cell missingness and identities."""

import json
import math
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec

import pandas as pd

from ashare_quant.factors.catalog import detail
from ashare_quant.storage.artifacts import digest, require


def validate_parameters(request, factor):
    allowed = {p["name"] for p in factor["parameters"]}
    require(
        not set(request.parameters) - allowed, "FACTOR_PARAMETERS", "Unexpected factor parameter"
    )
    for parameter in factor["parameters"]:
        value = request.parameters.get(parameter["name"])
        require(
            type(value) in (int, float) and math.isfinite(value),
            "FACTOR_PARAMETER_REQUIRED",
            parameter["label"] + "需要显式有限数值",
        )
        if parameter["kind"] == "integer":
            require(
                type(value) is int and parameter["minimum"] <= value <= parameter["maximum"],
                "FACTOR_PARAMETERS",
                "SMA m must be an integer in 1..60",
            )


def evaluation_available():
    try:
        return (
            find_spec("ashare_data.factors.evaluation") is not None
            and version("alphalens-reloaded") == "0.5.0"
        )
    except (ImportError, PackageNotFoundError):
        return False


def compute(frozen, request, job_id):
    factor = detail(request.factor_id, request.definition)
    validate_parameters(request, factor)
    data, kind = frozen["payload"], frozen["kind"]
    dates = [d for d in data["sessions"] if str(request.start_date) <= d <= str(request.end_date)]
    metadata = {}
    if kind == "alpha191":
        from ashare_data.factors.alpha191 import Alpha191

        axes = dict(index=pd.to_datetime(data["sessions"]), columns=data["securities"])
        frames = {f: pd.DataFrame(a, **axes, dtype=float) for f, a in data["fields"].items()}
        membership = pd.DataFrame(data["universe"], **axes, dtype=bool)
        extra = {
            k: pd.DataFrame(data[k], index=axes["index"], dtype=float) if data.get(k) else None
            for k in ("benchmark", "factors")
        }
        computed = Alpha191(
            frames,
            universe=membership,
            adjustment=data["adjustment"],
            units=data["units"],
            version=request.definition,
            rank_policy=request.rank_policy,
            input_id=digest(frozen),
            **request.parameters,
            **extra,
        ).compute([factor["field"]])
        sliced = computed.between(str(request.start_date), str(request.end_date))
        labels = sliced.status_labels()[factor["field"]]
        rows = [
            dict(
                date=day.date().isoformat(),
                security=sid,
                value=float(value) if math.isfinite(value) else None,
                value_text=None,
                status=labels.loc[(day, sid)],
                reason=None if labels.loc[(day, sid)] == "OK" else labels.loc[(day, sid)],
            )
            for (day, sid), value in sliced.values[factor["field"]].items()
        ]
        metadata = json.loads(json.dumps(computed.metadata, allow_nan=False))
        values = (
            computed.values[factor["field"]].unstack("security").reindex(columns=data["securities"])
        )
        statuses = computed.status_labels()[factor["field"]].unstack("security")
        values = values.where(statuses == "OK")
    else:
        if kind == "joinquant_panel":
            from ashare_data.factors.joinquant.panel import compute_panel

            computed_rows = [
                r
                for r in compute_panel(data, dates, definition=request.definition)
                if r["field"] == factor["field"]
            ]
        else:
            from ashare_data import FactorResearchView
            from ashare_data.factors.joinquant.research_view import MODE

            computed_rows = FactorResearchView(data).query(
                list(request.securities),
                [factor["field"]],
                dates=dates,
                definition=request.definition,
                mode=MODE,
            )["rows"]
        rows = [
            dict(
                date=r["date"],
                security=r["security"],
                value=float(r["value"]) if r["value"] is not None else None,
                value_text=r["value"],
                status=r["status"],
                reason=r.get("reason"),
            )
            for r in computed_rows
        ]
        values = pd.DataFrame(rows).pivot(index="date", columns="security", values="value")
        values.index = pd.to_datetime(values.index)
        membership = pd.DataFrame(
            True, index=pd.to_datetime(data["sessions"]), columns=request.securities
        )
    counts = Counter(r["status"] for r in rows)
    valid = [r["value"] for r in rows if r["value"] is not None]
    require(
        all(math.isfinite(v) for v in valid),
        "FACTOR_NONFINITE_RESULT",
        "Owner returned a nonfinite numerical value",
    )
    evaluation = None
    if request.evaluation is not None:
        require(
            evaluation_available(),
            "FACTOR_EVALUATION_UNAVAILABLE",
            "This installation does not include the evaluation module",
        )
        from ashare_quant.factors.evaluation_inputs import evaluate_frozen

        # Evaluation owns all label, lag, ranking and turnover semantics.
        evaluation = evaluate_frozen(values, membership, frozen, request)
    return dict(
        job_id=job_id,
        request=request.model_dump(mode="json"),
        factor=factor,
        provenance={
            **frozen["provenance"],
            "input_sha256": digest(frozen),
            "calculation": metadata,
        },
        rows=rows,
        summary=dict(
            total_cells=len(rows),
            valid_cells=len(valid),
            missing_cells=len(rows) - len(valid),
            coverage=len(valid) / len(rows) if rows else None,
            status_counts=dict(counts),
            minimum=min(valid) if valid else None,
            maximum=max(valid) if valid else None,
        ),
        evaluation=evaluation,
    )
