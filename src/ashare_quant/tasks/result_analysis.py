"""Project verified frozen evidence into the single pure analysis implementation."""

from ashare_quant.analysis import AnalysisInputError, AnalysisPolicy, analyze_result
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import digest, require

POLICY = AnalysisPolicy(annualization=252, risk_free_annual=0.0,
                        risk_free_source="assumed_zero", min_risk_observations=20)


def benchmark_input(result):
    frozen = result.get("native", {}).get("benchmark")
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


def analyze(result, snapshot_sha256):
    try:
        analysis = analyze_result(result, source_sha256=snapshot_sha256, policy=POLICY,
                                  benchmark=benchmark_input(result))
    except AnalysisInputError as exc:
        raise DomainError("RESULT_ANALYSIS_UNAVAILABLE", "Frozen result cannot support this analysis", 409,
                          details={"reason": exc.code}) from exc
    frozen = result.get("native", {}).get("benchmark")
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
    return analysis
