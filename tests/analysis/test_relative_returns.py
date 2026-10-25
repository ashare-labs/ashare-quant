"""Versioned relative NAV, with independent arithmetic and unchanged inputs."""

import copy
import json
import math
import statistics

import pytest

from ashare_quant.analysis import AnalysisInputError
from tests.analysis.test_returns import analyze, benchmark, result, value


@pytest.mark.parametrize("portfolio,index,excess,difference", [
    ([120], [110], 120 / 110 - 1, 10),
    ([110], [120], 110 / 120 - 1, -10),
    ([90], [80], 90 / 80 - 1, 10),
    ([80], [90], 80 / 90 - 1, -10),
])
def test_selected_definition_agrees_across_metric_series_and_month(portfolio, index, excess, difference):
    source = result(portfolio)
    bench = benchmark(index, source["preparation"]["trading_dates"])
    before = copy.deepcopy((source, bench))
    legacy = analyze(source, benchmark=bench)
    current = analyze(source, benchmark=bench, calculation_version="returns-v2")
    assert current["calculation_version"] == "returns-v2"
    assert current["policy"]["excess_return"] == "relative_nav_return"
    assert value(current, "excess_return") == pytest.approx(excess)
    assert current["series"][-1]["excess_return"] == pytest.approx(excess)
    assert current["monthly"][0]["excess_return"] == pytest.approx(excess)
    assert current["return_difference"]["value"] == pytest.approx(difference)
    assert current["return_difference"]["unit"] == "percentage_points"
    assert current["series"][-1]["return_difference"] == pytest.approx(difference)
    assert current["monthly"][0]["return_difference"] == pytest.approx(difference)
    assert value(legacy, "excess_return") == pytest.approx(difference / 100)
    assert "return_difference" not in legacy
    assert current["analysis_id"] != legacy["analysis_id"]
    assert current["source_sha256"] == legacy["source_sha256"]
    assert (source, bench) == before
    assert current["metrics"]["alpha"]["reason"] == "insufficient_samples"
    json.dumps(current, allow_nan=False)


def test_relative_drawdown_and_recovery_use_the_same_curve_with_initial_one():
    source = result([110, 120, 90, 110])
    bench = benchmark([110, 100, 100, 100], source["preparation"]["trading_dates"])
    out = analyze(source, benchmark=bench, calculation_version="returns-v2")
    relative = [1, 1, 1.2, .9, 1.1]
    expected = max(1 - level / max(relative[:i+1]) for i, level in enumerate(relative))
    assert [p["relative_nav"] for p in out["series"]] == pytest.approx(relative[1:])
    assert value(out, "excess_max_drawdown") == pytest.approx(expected)
    assert out["excess_drawdowns"][0]["depth"] == pytest.approx(.25)
    assert out["excess_drawdowns"][0]["peak_date"] == "2025-01-03"
    assert out["excess_drawdowns"][0]["trough_date"] == "2025-01-04"
    assert out["excess_drawdowns"][0]["recovery_date"] is None


def test_alpha_beta_and_information_ratio_are_not_replaced_by_relative_division():
    source = result([100, 100, 110, 99])
    bench = benchmark([101, 103, 102, 105], source["preparation"]["trading_dates"])
    old = analyze(source, benchmark=bench)
    new = analyze(source, benchmark=bench, calculation_version="returns-v2")
    rp, rb = [0, 0, .1, -.1], [.01, 103/101-1, 102/103-1, 105/102-1]
    active = [p-b for p, b in zip(rp, rb)]
    beta = statistics.covariance(rp, rb) / statistics.variance(rb)
    alpha = (1 + statistics.mean(rp) - beta * statistics.mean(rb)) ** 252 - 1
    ir = statistics.mean(active) / statistics.stdev(active) * math.sqrt(252)
    for key, expected in (("alpha", alpha), ("beta", beta), ("information_ratio", ir)):
        assert value(new, key) == pytest.approx(expected)
        assert new["metrics"][key] == old["metrics"][key]
    assert {k:v for k,v in new["metrics"].items() if k != "excess_return"} == {
        k:v for k,v in old["metrics"].items() if k != "excess_return"}


@pytest.mark.parametrize("kind", ["missing", "gap", "invalid", "underflow", "overflow"])
def test_absent_or_invalid_reference_has_no_invented_relative_return(kind):
    source = result([100, 100, 100])
    bench = benchmark([100, 100, 100], source["preparation"]["trading_dates"])
    if kind == "missing":
        bench = None
    elif kind == "gap":
        bench["points"].pop()
    elif kind == "invalid":
        bench["points"][0]["value"] = 0
    elif kind == "underflow":
        bench["points"][0]["value"] = 1e-300
    else:
        bench["initial_value"] = 1e-300
        bench["points"][0]["value"] = 1e300
    out = analyze(source, benchmark=bench, calculation_version="returns-v2")
    assert value(out, "net_return") == 0
    assert value(out, "excess_return") is None
    assert out["return_difference"]["value"] is None
    assert out["excess_drawdowns"] == []
    assert all(p["relative_nav"] is None and p["excess_return"] is None for p in out["series"])
    assert out["metrics"]["win_rate"]["reason"] == "no_closed_trades"


def test_daily_minute_and_bankruptcy_keep_defined_values_and_nulls():
    source = result([90, 0, 0])
    bench = benchmark([100, 102, 101], source["preparation"]["trading_dates"])
    minute = copy.deepcopy(source)
    minute["native"]["preflight"]["frequency"] = "1m"
    minute["native"]["accounts"].insert(0, dict(source["native"]["accounts"][0],
                                             at="2025-01-02T09:31:00+08:00", total_value=95))
    daily = analyze(source, benchmark=bench, calculation_version="returns-v2")
    intraday = analyze(minute, benchmark=bench, calculation_version="returns-v2")
    assert daily["series"] == intraday["series"]
    assert daily["metrics"] == intraday["metrics"]
    assert value(daily, "excess_return") == -1
    assert value(daily, "excess_max_drawdown") == 1
    assert daily["metrics"]["sharpe"]["reason"] == "zero_equity_base"
    json.dumps(daily, allow_nan=False)
    with pytest.raises(AnalysisInputError, match="unsupported_calculation_version"):
        analyze(source, calculation_version="unknown")
