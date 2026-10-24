"""Frozen daily benchmark observations, independent of orders and subscriptions.

This is an input projection for result analysis, not another ledger or metrics
engine. Missing reference prices leave strategy execution and its result intact.
"""

import math

from ashare_data.product.market_release import ReleasedDailyProduct

from ashare_quant.storage.artifacts import digest
from ashare_quant.tasks.minute.prepare import require


def configured_release(config):
    selected = config.document["profiles"][config.profile].get("benchmark_release")
    if selected is None:
        return None
    return {
        "root": str(config.asset(selected["asset"]).path),
        "release_id": selected["release_id"],
        "manifest_sha256": selected["manifest_sha256"],
    }


def observations(data, security, days, at):
    value = {
        "schema": "aq-benchmark-prices.v1", "security": security,
        "status": "not_selected" if security is None else "unavailable",
        "price_basis": "published_close_price_return", "base": None, "prices": [],
        "missing_dates": [], "reason": None, "release": None, "receipt": None,
        "historical_pit_verified": False,
        "limitations": ["Price return, not a dividend-reinvested total return index"],
    }
    if security is None:
        return value
    if not getattr(data, "demand", False):
        return {**value, "reason": "BENCHMARK_RELEASE_UNAVAILABLE"}
    require(days == data.days, "PYTHON_BENCHMARK_DATES", "Benchmark must match completed account sessions")
    from ashare_quant.backtests.minute.inputs import stamp
    require(stamp(days[-1] + "T15:00:00+08:00") <= at,
            "AQUANT_FUTURE_DATA_REQUEST", "Benchmark cannot include a future session close")
    base_date = data.previous[days[0]]
    wanted = [base_date, *days]
    data.check_budget(len(wanted))
    owner = data.owner
    if security not in owner.release.identifiers and data.benchmark_pin is not None:
        value["release"] = data.benchmark_pin
        try:
            owner = ReleasedDailyProduct({"market_release": data.benchmark_pin})
        except FileNotFoundError:
            return {**value, "reason": "BENCHMARK_SOURCE_UNAVAILABLE", "missing_dates": wanted}
    value["release"] = owner.release.receipt()
    if security not in owner.release.identifiers:
        return {**value, "reason": "BENCHMARK_SECURITY_UNAVAILABLE", "missing_dates": wanted}
    instrument = owner.release.instruments.get(security, {})
    value["asset_class"] = instrument.get("asset_class")
    try:
        response = owner.prices([security], base_date, days[-1])
    except FileNotFoundError:
        return {**value, "reason": "BENCHMARK_SOURCE_UNAVAILABLE", "missing_dates": wanted}
    rows = {r["date"]: r for r in response["rows"]}
    require(len(rows) == len(response["rows"]), "PYTHON_BENCHMARK_DUPLICATE", "Duplicate benchmark dates")
    missing, prices = [], []
    for day in wanted:
        row = rows.get(day)
        try:
            close = float(row["close"]) if row else None
            valid = close is not None and math.isfinite(close) and close > 0
        except (TypeError, ValueError, OverflowError):
            valid = False
        if valid:
            prices.append({"date": day, "close": close})
        else:
            missing.append(day)
    data.check_budget(len(response["rows"]))
    data.benchmark_rows = len(response["rows"])
    data._retain({
        "api": "benchmark_prices", "security": security, "start": base_date, "end": days[-1],
        "knowledge_at": at.isoformat(), "receipt": response["receipt"],
        "rows_sha256": digest(response["rows"]), "gaps": response["gaps"],
    }, len(response["rows"]))
    value.update(receipt=response["receipt"], missing_dates=missing,
                 price_unit=response["coverage"][0]["source_metadata"]["price_unit"])
    if missing:
        # Keep observed prices for diagnosis, but never make partial comparison
        # look complete by forward filling or dropping the strategy's dates.
        value.update(reason="BENCHMARK_BASE_MISSING" if base_date in missing else "BENCHMARK_DAILY_GAP",
                     observed_prices=prices)
    else:
        value.update(status="available", base=prices[0], prices=prices[1:])
    data.queries.append({"api": "benchmark_result", "security": security,
                         "rows": len(prices), "result_sha256": digest(value)})
    return value
