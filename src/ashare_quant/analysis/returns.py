"""Versioned daily portfolio statistics, calculated without rerunning a strategy."""

import hashlib
import json
import math
import re
import warnings
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import version

import numpy as np

from .policy import AnalysisInputError, AnalysisPolicy, metric, number, session
from .trading import analyze_trading

METRICS = (
    "net_return", "annual_return", "excess_return", "benchmark_return", "alpha", "beta",
    "sharpe", "win_rate", "profit_loss_ratio", "max_drawdown", "sortino",
    "daily_excess_return", "excess_max_drawdown", "excess_sharpe", "daily_win_rate",
    "winning_trades", "losing_trades", "information_ratio", "volatility", "benchmark_volatility",
)
RATIOS = {"alpha", "beta", "sharpe", "sortino", "profit_loss_ratio", "information_ratio",
          "excess_sharpe"}


def analyze_result(result, *, source_sha256: str, policy: AnalysisPolicy, benchmark=None,
                   calculation_version="returns-v1"):
    """Analyze a verified result mapping. Inputs are never mutated; no I/O is performed.

    The caller owns verification of source_sha256 and frozen benchmark provenance.
    Critical account inconsistencies raise AnalysisInputError. Missing/invalid benchmark
    and unsupported trade attribution leave independent portfolio statistics available.
    """
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise AnalysisInputError("invalid_source_sha256")
    if calculation_version not in {"returns-v1", "returns-v2"}:
        raise AnalysisInputError("unsupported_calculation_version")
    relative = calculation_version == "returns-v2"
    import empyrical as ep

    engine_version = version("empyrical-reloaded")
    if engine_version != "0.7.0":
        raise AnalysisInputError("unsupported_analysis_engine_version")
    native, summary = result.get("native", {}), result.get("summary", {})
    initial = number(summary.get("initial_cash"))
    if initial is None or initial <= 0:
        raise AnalysisInputError("missing_initial_equity")
    if native.get("external_cash_flows"):
        raise AnalysisInputError("unsupported_external_cash_flows")
    preparation = result.get("preparation", {})
    daily = _daily_accounts(native.get("accounts"), preparation)
    has_calendar = isinstance(preparation.get("trading_dates"), list)
    n, a = len(daily), policy.annualization
    values = np.array([initial, *[row["equity"] for row in daily]], dtype=float)
    with np.errstate(over="ignore"):
        nav = values / initial
    if not np.isfinite(nav).all():
        raise AnalysisInputError("equity_scale_overflow")
    if any(before == 0 and after > 0 for before, after in zip(values, values[1:])):
        raise AnalysisInputError("unsupported_external_cash_flows")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = values[1:] / values[:-1] - 1
    valid_returns = bool(np.isfinite(returns).all())
    curve = nav / np.maximum.accumulate(nav) - 1
    metrics = {key: metric(unit="ratio" if key in RATIOS else "percent",
                           reason="not_computed", count=n) for key in METRICS}

    def put(key, value=None, reason=None):
        metrics[key] = metric(value, unit="ratio" if key in RATIOS else "percent",
                              reason=reason, count=n)

    def calculate(fn, *args, **kwargs):
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            return number(fn(*args, **kwargs))

    total = calculate(ep.cum_returns_final, returns) if valid_returns else nav[-1] - 1
    depth = -calculate(ep.max_drawdown, returns) if valid_returns else -min(curve)
    for key, value in (("net_return", total), ("max_drawdown", depth)):
        recorded = number(summary.get(key))
        if recorded is not None and not math.isclose(recorded, value, abs_tol=1e-10, rel_tol=1e-8):
            raise AnalysisInputError("inconsistent_frozen_summary")
        put(key, recorded if recorded is not None else value)
    base_reason = ("zero_equity_base" if not valid_returns else
                   "missing_session_calendar" if not has_calendar else "insufficient_samples"
                   if n < policy.min_risk_observations else None)
    rf = (None if policy.risk_free_annual is None else
          (1 + policy.risk_free_annual) ** (1 / a) - 1)
    for key in ("annual_return", "volatility", "sharpe", "sortino"):
        put(key, reason=base_reason or "missing_risk_free_rate")
    if not base_reason:
        put("annual_return", calculate(ep.annual_return, returns, annualization=a))
        volatility = calculate(ep.annual_volatility, returns, annualization=a)
        put("volatility", volatility)
        if rf is not None:
            if float(np.std(returns, ddof=1)) <= 1e-15:
                put("sharpe", reason="zero_variance")
            else:
                put("sharpe", calculate(ep.sharpe_ratio, returns, risk_free=rf, annualization=a))
            if np.all(returns >= rf):
                put("sortino", reason="no_downside_returns")
            else:
                put("sortino", calculate(ep.sortino_ratio, returns, required_return=rf,
                                          annualization=a))
    put("daily_win_rate", float(np.count_nonzero(returns > 0) / n) if valid_returns else None,
        None if valid_returns else "zero_equity_base")

    benchmark_returns, benchmark_info, benchmark_reason = _benchmark(benchmark, daily)
    benchmark_nav = relative_nav = None
    benchmark_metrics = ("benchmark_return", "excess_return", "alpha", "beta",
                         "daily_excess_return", "excess_max_drawdown", "excess_sharpe",
                         "information_ratio", "benchmark_volatility")
    for key in benchmark_metrics:
        put(key, reason=benchmark_reason or base_reason or "not_computed")
    if benchmark_returns is not None:
        benchmark_nav = np.cumprod(1 + benchmark_returns)
        put("benchmark_return", calculate(ep.cum_returns_final, benchmark_returns))
        relative_nav = nav / np.r_[1.0, benchmark_nav]
        put("excess_return", float(relative_nav[-1] - 1) if relative else
            total - metrics["benchmark_return"]["value"])
        put("excess_max_drawdown", float(-min(relative_nav / np.maximum.accumulate(relative_nav) - 1)))
        if valid_returns:
            active = returns - benchmark_returns
            put("daily_excess_return", float(np.mean(active)))
            if not base_reason:
                put("benchmark_volatility", calculate(ep.annual_volatility, benchmark_returns,
                                                       annualization=a))
                if float(np.std(benchmark_returns, ddof=1)) <= 1e-15:
                    put("beta", reason="zero_benchmark_variance")
                    put("alpha", reason="zero_benchmark_variance")
                else:
                    put("beta", calculate(ep.beta, returns, benchmark_returns))
                    put("alpha", calculate(ep.alpha, returns, benchmark_returns, risk_free=rf,
                                           annualization=a) if rf is not None else None,
                        None if rf is not None else "missing_risk_free_rate")
                if float(np.std(active, ddof=1)) <= 1e-15:
                    put("information_ratio", reason="zero_tracking_error")
                    put("excess_sharpe", reason="zero_tracking_error")
                else:
                    ir = calculate(ep.excess_sharpe, returns, benchmark_returns)
                    put("information_ratio", ir * math.sqrt(a) if ir is not None else None)
                    # Original labels retained; v1 explicitly treats these as aliases.
                    metrics["excess_sharpe"] = dict(metrics["information_ratio"])

    trading, costs, flows, trade_metrics = analyze_trading(native, daily, initial)
    metrics.update(trade_metrics)
    series = []
    for index, row in enumerate(daily):
        benchmark_cumulative = float(benchmark_nav[index] - 1) if benchmark_nav is not None else None
        difference = (float(nav[index + 1] - 1) - benchmark_cumulative
                      if benchmark_cumulative is not None else None)
        excess = (float(relative_nav[index + 1] - 1)
                  if relative_nav is not None and relative else difference)
        series.append({"date": row["date"], "equity": row["equity"], "cash": row["cash"],
                       "positions_value": row["positions_value"], "return": number(returns[index]),
                       "pnl": float(values[index + 1] - values[index]), "nav": float(nav[index + 1]),
                       "cumulative_return": float(nav[index + 1] - 1),
                       "benchmark_return": float(benchmark_returns[index])
                       if benchmark_returns is not None else None,
                       "benchmark_cumulative_return": benchmark_cumulative,
                       "excess_return": excess,
                       "drawdown": float(curve[index + 1]), **flows[row["date"]],
                       **({"return_difference": difference * 100 if difference is not None else None,
                           "relative_nav": float(relative_nav[index + 1]) if relative_nav is not None else None}
                          if relative else {})})
    monthly = _monthly(series, ep, relative=relative)
    rolling = []
    if valid_returns:
        for window in (20, 60, 120):
            for end in range(window, n + 1):
                sample = returns[end - window:end]
                eligible = has_calendar and window >= policy.min_risk_observations
                rolling.append({"date": daily[end - 1]["date"], "window": window,
                                "return": calculate(ep.cum_returns_final, sample),
                                "volatility": calculate(ep.annual_volatility, sample, annualization=a)
                                if eligible else None,
                                "sharpe": calculate(ep.sharpe_ratio, sample, risk_free=rf,
                                                    annualization=a)
                                if eligible and rf is not None and np.std(sample, ddof=1) > 1e-15
                                else None})
    reasons = sorted({m["reason"] for m in metrics.values() if m["reason"]})
    identity = {"source_sha256": source_sha256, "policy": asdict(policy),
                "benchmark": benchmark, "calculation_version": calculation_version, "engine": engine_version}
    # Invalid benchmark payloads must not prevent independent portfolio metrics.
    identity_bytes = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                allow_nan=True, default=str).encode()
    return {
        "schema_version": "aq.analysis.v1", "calculation_version": calculation_version,
        "analysis_id": hashlib.sha256(identity_bytes).hexdigest(),
        "job_id": result.get("job_id"), "source_sha256": source_sha256,
        "engine": {"name": "empyrical-reloaded", "version": engine_version},
        "policy": {**asdict(policy), "evaluation_frequency": "1d", "timezone": "Asia/Shanghai",
                   "currency": "CNY", "cash_flows": "initial_cash_only",
                   "sampling": "last_account_per_session", "drawdown_sign": "negative_series_positive_metric",
                   "daily_win_rate": "positive_sessions_over_all_valid_sessions",
                   "alpha": "daily_jensen_intercept_compounded_annually",
                   "excess_return": "relative_nav_return" if relative else "cumulative_return_difference",
                   "excess_max_drawdown": "relative_nav_drawdown",
                   "information_ratio": "mean_active_over_sample_tracking_error_times_sqrt_annualization",
                   "excess_sharpe": "alias_of_information_ratio",
                   **({"return_difference": "cumulative_return_difference_in_percentage_points"}
                      if relative else {})},
        "initial_equity": initial, "sample_count": n,
        "metrics": {key: metrics[key] for key in METRICS}, "series": series,
        "benchmark": benchmark_info, "drawdowns": _drawdowns(daily, values),
        "monthly": monthly, "rolling": rolling, "costs": costs, "trading": trading,
        **({"return_difference": metric(
                (total - metrics["benchmark_return"]["value"]) * 100
                if benchmark_nav is not None else None, unit="percentage_points",
                reason=benchmark_reason, count=n),
            "excess_drawdowns": _drawdowns(daily, relative_nav) if relative_nav is not None else []}
           if relative else {}),
        "quality": {"status": "partial" if reasons else "ok", "reasons": reasons,
                    "calendar_source": "frozen_sessions" if has_calendar else "observed_accounts_only",
                    "historical_pit_verified": native.get("historical_pit_verified"),
                    "corporate_account_model": native.get("corporate_account_model"),
                    "execution_frequency": native.get("preflight", {}).get("frequency")},
    }


def _daily_accounts(accounts, preparation):
    if not isinstance(accounts, list) or not accounts:
        raise AnalysisInputError("missing_account_series")
    daily, previous, seen = {}, None, set()
    for account in accounts:
        day = session(account.get("at"))
        raw = account["at"]
        stamp = datetime.fromisoformat(raw) if len(raw) > 10 else datetime.fromisoformat(raw + "T00:00:00+08:00")
        if stamp in seen or (previous is not None and stamp < previous):
            raise AnalysisInputError("unordered_or_duplicate_accounts")
        seen.add(stamp)
        previous = stamp
        equity = number(account.get("total_value"))
        if equity is None or equity < 0:
            raise AnalysisInputError("invalid_equity")
        daily[day] = {"date": day, "equity": equity,
                      "cash": number(account.get("available_cash")),
                      "locked_cash": number(account.get("locked_cash")),
                      "positions_value": number(account.get("positions_value")),
                      "positions": account.get("positions")}
    expected = preparation.get("trading_dates")
    if expected is not None and expected != list(daily):
        raise AnalysisInputError("account_session_coverage_mismatch")
    return list(daily.values())


def _benchmark(benchmark, daily):
    if benchmark is None:
        return None, None, "missing_benchmark"
    if not isinstance(benchmark, dict):
        return None, {"status": "unavailable", "reason": "invalid_benchmark"}, "invalid_benchmark"
    info = {key: benchmark.get(key) if isinstance(benchmark.get(key), str) else None
            for key in ("security", "name", "return_type", "source_sha256")}
    reason = None
    try:
        initial = number(benchmark.get("initial_value"))
        points = benchmark.get("points")
        if (initial is None or initial <= 0 or not isinstance(points, list)
                or not isinstance(info["security"], str) or not info["security"]
                or info["return_type"] not in {"price", "total_return"}
                or not isinstance(info["source_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", info["source_sha256"])):
            raise AnalysisInputError("invalid_benchmark")
        if session(benchmark.get("initial_date")) >= daily[0]["date"]:
            raise AnalysisInputError("invalid_benchmark_initial_date")
        dates = [session(point.get("date")) for point in points]
        if dates != [row["date"] for row in daily]:
            raise AnalysisInputError("benchmark_session_mismatch")
        values = [number(point.get("value")) for point in points]
        if any(value is None or value <= 0 for value in values):
            raise AnalysisInputError("invalid_benchmark_value")
        levels = np.array([initial, *values])
        returns = levels[1:] / levels[:-1] - 1
        return returns, {**info, "status": "ok", "reason": None}, None
    except (AnalysisInputError, AttributeError) as exc:
        reason = exc.code if isinstance(exc, AnalysisInputError) else "invalid_benchmark"
    return None, {**info, "status": "unavailable", "reason": reason}, reason


def _monthly(series, ep, *, relative=False):
    groups = defaultdict(list)
    for row in series:
        groups[row["date"][:7]].append(row)
    output = []
    for month, rows in groups.items():
        returns = [row["return"] for row in rows]
        bench = [row["benchmark_return"] for row in rows]
        value = number(ep.cum_returns_final(np.asarray(returns))) if None not in returns else None
        b_value = number(ep.cum_returns_final(np.asarray(bench))) if None not in bench else None
        difference = value - b_value if value is not None and b_value is not None else None
        excess = (number((1 + value) / (1 + b_value) - 1)
                  if relative and difference is not None and b_value > -1 else difference)
        output.append({"month": month, "return": value, "benchmark_return": b_value,
                       "excess_return": excess,
                       **({"return_difference": difference * 100 if difference is not None else None}
                          if relative else {}),
                       "boundary_month": month in {series[0]["date"][:7], series[-1]["date"][:7]}})
    return output


def _drawdowns(daily, values):
    dates = [None, *[row["date"] for row in daily]]
    peak, start, trough, episodes = 0, None, None, []

    def episode(recovery):
        return {"peak_date": dates[peak], "trough_date": dates[trough],
                "recovery_date": dates[recovery] if recovery is not None else None,
                "depth": float(1 - values[trough] / values[peak]),
                "decline_sessions": trough - peak,
                "recovery_sessions": recovery - trough if recovery is not None else None,
                "underwater_sessions": (recovery if recovery is not None else len(values) - 1) - peak,
                "observed_through": dates[-1] if recovery is None else dates[recovery]}

    for index in range(1, len(values)):
        if values[index] >= values[peak]:
            if start is not None:
                episodes.append(episode(index))
            peak, start, trough = index, None, None
        else:
            if start is None:
                start = trough = index
            elif values[index] < values[trough]:
                trough = index
    if start is not None:
        episodes.append(episode(None))
    return sorted(episodes, key=lambda row: row["depth"], reverse=True)
