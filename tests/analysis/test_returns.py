"""Independent arithmetic fixtures; no services, market downloads or user strategies."""

import copy
import json
import math
import socket
import statistics
from datetime import date, timedelta

import pytest

from ashare_quant.analysis import AnalysisInputError, AnalysisPolicy, analyze_result

SHA = "0" * 64
POLICY = AnalysisPolicy(annualization=252, risk_free_annual=0.0,
                        risk_free_source="assumed_zero", min_risk_observations=2)


def result(equities, *, dates=None, positions=None, frequency="1d"):
    dates = dates or [(date(2025, 1, 2) + timedelta(days=i)).isoformat()
                      for i in range(len(equities))]
    return {"job_id": "synthetic-test-only", "summary": {"initial_cash": 100.0},
            "preparation": {"trading_dates": dates},
            "native": {"preflight": {"frequency": frequency}, "trades": [], "orders": [],
                       "accounts": [{"at": day + "T15:30:00+08:00", "total_value": equity,
                                     "available_cash": equity, "positions_value": 0,
                                     "positions": positions[i] if positions else {}}
                                    for i, (day, equity) in enumerate(zip(dates, equities))]}}


def analyze(value, **kwargs):
    return analyze_result(value, source_sha256=SHA, policy=kwargs.pop("policy", POLICY), **kwargs)


def value(out, key):
    return out["metrics"][key]["value"]


def benchmark(levels, dates):
    return {"security": "TEST_INDEX", "name": "Synthetic index", "return_type": "price",
            "source_sha256": "1" * 64, "initial_date": "2024-12-31", "initial_value": 100,
            "points": [{"date": d, "value": v} for d, v in zip(dates, levels)]}


def test_first_day_costs_anchor_compounding_and_drawdown_are_hand_calculated():
    source = result([99, 110, 88, 110, 100])
    before = copy.deepcopy(source)
    out = analyze(source)
    expected = [-0.01, 110 / 99 - 1, -0.2, 0.25, 100 / 110 - 1]
    assert [x["return"] for x in out["series"]] == pytest.approx(expected)
    assert [x["pnl"] for x in out["series"]] == [-1, 11, -22, 22, -10]
    assert value(out, "net_return") == pytest.approx(0)
    assert value(out, "max_drawdown") == pytest.approx(0.2)
    assert value(out, "annual_return") == pytest.approx(0)
    assert value(out, "volatility") == pytest.approx(statistics.stdev(expected) * math.sqrt(252))
    assert value(out, "sharpe") == pytest.approx(statistics.mean(expected) / statistics.stdev(expected) * math.sqrt(252))
    downside = math.sqrt(sum(min(x, 0) ** 2 for x in expected) / len(expected))
    assert value(out, "sortino") == pytest.approx(statistics.mean(expected) / downside * math.sqrt(252))
    assert out["drawdowns"][0] == {"peak_date": "2025-01-03", "trough_date": "2025-01-04",
                                    "recovery_date": "2025-01-05", "depth": pytest.approx(.2),
                                    "decline_sessions": 1, "recovery_sessions": 1,
                                    "underwater_sessions": 2, "observed_through": "2025-01-05"}
    assert out["drawdowns"][1]["recovery_date"] is None
    assert source == before
    json.dumps(out, allow_nan=False)


def test_missing_benchmark_keeps_strategy_statistics_and_does_not_fill_zero():
    out = analyze(result([101, 99, 102]))
    assert value(out, "sharpe") is not None
    for key in ("benchmark_return", "alpha", "beta", "excess_return", "information_ratio"):
        assert out["metrics"][key]["value"] is None
        assert out["metrics"][key]["reason"] == "missing_benchmark"
    assert all(row["benchmark_return"] is None for row in out["series"])


def test_constant_cash_has_real_zero_return_but_no_fabricated_trade_ratios():
    out = analyze(result([100, 100, 100]))
    assert value(out, "net_return") == 0
    assert value(out, "max_drawdown") == 0
    assert value(out, "volatility") == 0
    assert out["metrics"]["sharpe"]["reason"] == "zero_variance"
    assert out["metrics"]["sortino"]["reason"] == "no_downside_returns"
    assert value(out, "daily_win_rate") == 0
    assert value(out, "winning_trades") == value(out, "losing_trades") == 0
    assert out["metrics"]["win_rate"]["reason"] == "no_closed_trades"
    assert out["costs"]["total"] == 0


def test_single_point_short_sample_and_missing_risk_free_are_explicit():
    out = analyze(result([99]))
    assert value(out, "net_return") == pytest.approx(-.01)
    assert out["drawdowns"][0]["peak_date"] is None  # Initial capital, not an invented date.
    assert out["drawdowns"][0]["recovery_sessions"] is None
    for key in ("annual_return", "volatility", "sharpe", "sortino"):
        assert out["metrics"][key]["reason"] == "insufficient_samples"
    policy = AnalysisPolicy(annualization=244, risk_free_annual=None,
                            risk_free_source="not_configured", min_risk_observations=2)
    out = analyze(result([99, 101]), policy=policy)
    assert value(out, "annual_return") is not None
    assert value(out, "volatility") is not None
    assert out["metrics"]["sharpe"]["reason"] == "missing_risk_free_rate"


def test_daily_and_minute_account_sampling_match_without_minute_annualization():
    daily = result([101, 99, 102])
    minute = copy.deepcopy(daily)
    minute["native"]["preflight"]["frequency"] = "1m"
    minute["native"]["accounts"] = [r for account in daily["native"]["accounts"]
                                      for r in [dict(account, at=account["at"].replace("15:30", "09:31"),
                                                     total_value=100), account]]
    one, two = analyze(daily), analyze(minute)
    assert one["metrics"] == two["metrics"]
    assert one["series"] == two["series"]
    assert two["policy"]["evaluation_frequency"] == "1d"
    assert two["sample_count"] == 3


def test_cash_waiting_days_are_not_trimmed_when_benchmark_is_present():
    source = result([100, 100, 110, 99])
    bench = benchmark([101, 103, 102, 105], source["preparation"]["trading_dates"])
    out = analyze(source, benchmark=bench)
    rp = [0, 0, .1, -.1]
    rb = [.01, 103 / 101 - 1, 102 / 103 - 1, 105 / 102 - 1]
    active = [p - b for p, b in zip(rp, rb)]
    beta = statistics.covariance(rp, rb) / statistics.variance(rb)
    alpha_daily = statistics.mean(rp) - beta * statistics.mean(rb)
    assert out["sample_count"] == 4
    assert value(out, "net_return") == pytest.approx(-.01)
    assert value(out, "benchmark_return") == pytest.approx(.05)
    assert value(out, "excess_return") == pytest.approx(-.06)
    assert value(out, "beta") == pytest.approx(beta)
    assert value(out, "alpha") == pytest.approx((1 + alpha_daily) ** 252 - 1)
    assert value(out, "information_ratio") == pytest.approx(statistics.mean(active) / statistics.stdev(active) * math.sqrt(252))
    assert value(out, "information_ratio") == value(out, "excess_sharpe")
    assert out["policy"]["excess_sharpe"] == "alias_of_information_ratio"
    relative = [1, 100/101, 100/103, 110/102, 99/105]
    expected_dd = max(1 - x / max(relative[:i + 1]) for i, x in enumerate(relative))
    assert value(out, "excess_max_drawdown") == pytest.approx(expected_dd)


def test_bad_benchmark_alignment_does_not_shift_or_drop_strategy_dates():
    source = result([100, 110, 99])
    bench = benchmark([110, 99], source["preparation"]["trading_dates"][1:])
    out = analyze(source, benchmark=bench)
    assert len(out["series"]) == 3
    assert value(out, "net_return") == pytest.approx(-.01)
    assert out["metrics"]["benchmark_return"]["reason"] == "benchmark_session_mismatch"


def test_rf_conversion_and_loss_only_sortino_match_independent_calculation():
    p = AnalysisPolicy(annualization=244, risk_free_annual=.03,
                       risk_free_source="explicit_test_assumption", min_risk_observations=2)
    out = analyze(result([99, 97, 94]), policy=p)
    rf = 1.03 ** (1 / 244) - 1
    excess = [.99 - 1 - rf, 97/99 - 1 - rf, 94/97 - 1 - rf]
    assert value(out, "sharpe") == pytest.approx(statistics.mean(excess) / statistics.stdev(excess) * math.sqrt(244))
    assert value(out, "sortino") == pytest.approx(statistics.mean(excess) / math.sqrt(sum(x*x for x in excess)/3) * math.sqrt(244))
    assert value(out, "annual_return") == pytest.approx(.94 ** (244/3) - 1)


def test_monthly_compounding_and_rolling_window_do_not_sum_returns():
    dates = [(date(2025, 1, 15) + timedelta(days=i)).isoformat() for i in range(25)]
    levels = [100 * (1.01 ** (i + 1)) for i in range(25)]
    out = analyze(result(levels, dates=dates))
    assert out["monthly"][0]["return"] == pytest.approx(1.01**17 - 1)
    assert out["monthly"][1]["return"] == pytest.approx(1.01**8 - 1)
    assert len(out["rolling"]) == 6
    assert out["rolling"][0]["return"] == pytest.approx(1.01**20 - 1)
    assert out["rolling"][0]["sharpe"] is None


@pytest.mark.parametrize("mutation,code", [
    (lambda x: x["native"]["accounts"].reverse(), "unordered_or_duplicate_accounts"),
    (lambda x: x["native"]["accounts"].append(x["native"]["accounts"][-1]), "unordered_or_duplicate_accounts"),
    (lambda x: x["native"]["accounts"].pop(), "account_session_coverage_mismatch"),
    (lambda x: x["native"]["accounts"][0].update(total_value=float("nan")), "invalid_equity"),
    (lambda x: x["summary"].update(net_return=.99), "inconsistent_frozen_summary"),
    (lambda x: x["native"].update(external_cash_flows=[{"amount": 10}]), "unsupported_external_cash_flows"),
])
def test_invalid_or_incomplete_account_data_is_not_silently_repaired(mutation, code):
    source = result([101, 99, 102])
    mutation(source)
    with pytest.raises(AnalysisInputError, match=code):
        analyze(source)


def test_bankruptcy_never_serializes_nan_or_infinity():
    out = analyze(result([90, 0, 0]))
    assert value(out, "net_return") == -1
    assert value(out, "max_drawdown") == 1
    assert out["metrics"]["sharpe"]["reason"] == "zero_equity_base"
    json.dumps(out, allow_nan=False)


def test_analysis_identity_is_deterministic_and_policy_sensitive_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Analysis must not connect to a data provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    source = result([101, 99, 103])
    one, two = analyze(source), analyze(copy.deepcopy(source))
    assert one == two
    changed = AnalysisPolicy(annualization=244, risk_free_annual=0,
                             risk_free_source="assumed_zero", min_risk_observations=2)
    assert analyze(source, policy=changed)["analysis_id"] != one["analysis_id"]


def test_equivalent_duplicate_timestamps_are_rejected():
    source = result([101, 99])
    source["native"]["accounts"].insert(1, dict(source["native"]["accounts"][0],
                                               at="2025-01-02T07:30:00Z"))
    with pytest.raises(AnalysisInputError, match="unordered_or_duplicate_accounts"):
        analyze(source)


def test_missing_calendar_does_not_claim_complete_risk_observations():
    source = result([101, 99, 103])
    source.pop("preparation")
    out = analyze(source)
    assert value(out, "net_return") == pytest.approx(.03)
    assert out["metrics"]["sharpe"]["reason"] == "missing_session_calendar"
    assert out["quality"]["calendar_source"] == "observed_accounts_only"
