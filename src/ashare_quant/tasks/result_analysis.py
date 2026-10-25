"""Project verified frozen evidence into the single pure analysis implementation."""

from ashare_quant.analysis import AnalysisInputError, AnalysisPolicy, analyze_result
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import digest, require

POLICY = AnalysisPolicy(annualization=252, risk_free_annual=0.0,
                        risk_free_source="assumed_zero", min_risk_observations=20)


def benchmark_input(result, *, evidence=None):
    frozen = result.get("native", {}).get("benchmark") if evidence is None else evidence
    if frozen is None:
        return None
    require(isinstance(frozen, dict), "RESULT_BENCHMARK_UNSUPPORTED", "Invalid frozen benchmark")
    if frozen.get("status") == "not_selected":
        return None
    require(frozen.get("schema") == "aq-benchmark-prices.v1",
            "RESULT_BENCHMARK_UNSUPPORTED", "Unknown frozen benchmark schema")
    if frozen.get("status") == "unavailable":
        return None
    require(frozen.get("status") == "available"
            and frozen.get("price_basis") == "published_close_price_return",
            "RESULT_BENCHMARK_UNSUPPORTED", "Unknown frozen benchmark price basis")
    try:
        return {
            "security": frozen["security"], "name": frozen["security"], "return_type": "price",
            "source_sha256": digest(frozen), "initial_date": frozen["base"]["date"],
            "initial_value": frozen["base"]["close"],
            "points": [{"date": row["date"], "value": row["close"]} for row in frozen["prices"]],
        }
    except (KeyError, TypeError) as exc:
        raise DomainError("RESULT_BENCHMARK_UNSUPPORTED", "Invalid frozen benchmark prices", 409) from exc


def analyze(result, snapshot_sha256, *, calculation_version="returns-v1", comparison=None):
    frozen = result.get("native", {}).get("benchmark")
    metadata = None
    if comparison is not None:
        frozen, metadata = comparison
    projected = benchmark_input(result, evidence=frozen)
    if projected is not None and calculation_version == "returns-v2":
        from ashare_quant.tasks.benchmark_comparison import name

        projected["name"] = name(frozen.get("security"), frozen.get("name"))
    try:
        analysis = analyze_result(result, source_sha256=snapshot_sha256, policy=POLICY,
                                  benchmark=projected, calculation_version=calculation_version)
    except AnalysisInputError as exc:
        raise DomainError("RESULT_ANALYSIS_UNAVAILABLE", "Frozen result cannot support this analysis", 409,
                          details={"reason": exc.code}) from exc
    # Keep the actual execution-time data gap, rather than relabeling it as an
    # unconfigured benchmark. No price fetching or metric recomputation occurs.
    if frozen and frozen.get("status") == "unavailable":
        reason = frozen.get("reason") or "BENCHMARK_SOURCE_UNAVAILABLE"
        analysis["benchmark"] = {
            "security": frozen.get("security"), "name": frozen.get("security"),
            "return_type": "price", "source_sha256": digest(frozen),
            "status": "unavailable", "reason": reason, "missing_dates": frozen.get("missing_dates", []),
        }
        for value in analysis["metrics"].values():
            if value["reason"] == "missing_benchmark":
                value["reason"] = reason
        analysis["quality"]["reasons"] = sorted({v["reason"] for v in analysis["metrics"].values() if v["reason"]})
    if calculation_version == "returns-v2":
        if frozen and frozen.get("status") == "not_selected" and frozen.get("selection_source"):
            reason = "benchmark_disabled"
            for value in analysis["metrics"].values():
                if value["reason"] == "missing_benchmark":
                    value["reason"] = reason
            analysis["quality"]["reasons"] = sorted({v["reason"] for v in analysis["metrics"].values() if v["reason"]})
        if analysis["benchmark"] is not None and frozen:
            analysis["benchmark"].update(selection_source=frozen.get("selection_source", "strategy"),
                                         release=frozen.get("release"))
        if analysis["return_difference"]["value"] is None:
            analysis["return_difference"]["reason"] = analysis["metrics"]["excess_return"]["reason"]
        if metadata is not None:
            analysis["comparison"] = metadata
            analysis["analysis_id"] = digest({"calculation": analysis["analysis_id"],
                                               "selection": metadata["selection"],
                                               "benchmark_evidence_sha256": digest(frozen)})
    return analysis
