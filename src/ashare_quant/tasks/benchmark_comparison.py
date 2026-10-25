"""Read-only reference choices from a task's pinned publications, never latest."""

from ashare_data.core.model import DataError
from ashare_data.product.market_release import ReleasedDailyProduct

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import digest
from ashare_quant.tasks.minute.benchmark import empty_observations, published_observations

NAMES = {"000300.XSHG": "沪深300", "000001.XSHG": "上证指数"}


def name(security, supplied=None):
    return NAMES.get(security) or supplied or security


def comparison(result, session, selection):
    """Return new benchmark evidence and metadata; never mutate frozen inputs.

    The market publication supplies the evaluation calendar. A supplemental
    publication supplies indices absent from that market, as at execution time.
    A missing publication is reported, not replaced with a current release.
    """
    original = result.get("native", {}).get("benchmark")
    metadata = {
        "selection": selection, "source": "frozen" if selection == "frozen" else "derived",
        "original_benchmark_sha256": digest(original),
        "original_security": original.get("security") if original else None,
        "options": [{"value": "frozen", "label": "原始基准"}, {"value": "none", "label": "无基准"}],
        "reference_status": "available", "reference_reason": None,
        "release_policy": "task_frozen_publications_only",
    }
    owners = []
    try:
        for pin in (session.get("release"), session.get("benchmark_release")):
            if pin and pin not in [p for p, _ in owners]:
                owners.append((pin, ReleasedDailyProduct({"market_release": pin})))
    except FileNotFoundError:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_SOURCE_UNAVAILABLE")
    except DataError:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_SOURCE_INVALID")
    if not owners and metadata["reference_reason"] is None:
        metadata.update(reference_status="unavailable", reference_reason="BENCHMARK_RELEASE_UNAVAILABLE")
    choices = {}
    for _, owner in owners:
        for security, instrument in owner.release.instruments.items():
            if instrument.get("asset_class") == "index":
                choices.setdefault(security, {"value": security, "label": name(security, instrument.get("name"))})
    metadata["options"].extend(choices[key] for key in sorted(choices))
    if selection == "frozen":
        return original, metadata
    value = empty_observations(None if selection == "none" else selection, "comparison")
    if selection == "none":
        return value, metadata
    # Use the first publication (the task's original market) for the preceding
    # session. Taking the previous observed index row would hide a missing base.
    market = next((owner for pin, owner in owners if pin == session.get("release")), None)
    if market is None:
        return {**value, "reason": metadata["reference_reason"] or "BENCHMARK_CALENDAR_UNAVAILABLE"}, metadata
    days = result.get("preparation", {}).get("trading_dates")
    if not isinstance(days, list) or not days or any(not isinstance(day, str) for day in days):
        return {**value, "reason": "BENCHMARK_CALENDAR_UNAVAILABLE"}, metadata
    try:
        with market.release.metadata() as db:
            prior = db.execute("SELECT date FROM calendar WHERE date < ? ORDER BY date DESC LIMIT 1",
                               [days[0]]).fetchone()
        if prior is None:
            return {**value, "reason": "BENCHMARK_BASE_MISSING", "missing_dates": []}, metadata
        value, _ = published_observations(market, selection, days, prior[0],
                                          supplemental_pin=session.get("benchmark_release"))
    except FileNotFoundError:
        value.update(reason="BENCHMARK_SOURCE_UNAVAILABLE")
    except DataError:
        value.update(reason="BENCHMARK_SOURCE_INVALID")
    value["name"] = name(selection, value.get("name"))
    return value, metadata


def check_query(request, analysis_version, benchmark):
    allowed = {"analysis_version", "benchmark"}
    if (set(request.query_params) - allowed
            or any(len(request.query_params.getlist(key)) > 1 for key in allowed)):
        raise DomainError("RESULT_ANALYSIS_QUERY_UNSUPPORTED", "Use only documented analysis fields", 422)
    if analysis_version == "returns-v1" and benchmark != "frozen":
        raise DomainError("RESULT_ANALYSIS_QUERY_UNSUPPORTED", "Comparison requires returns-v2", 422)
