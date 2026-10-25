"""Pinned publication comparisons over retained accounts; no strategy execution."""

import json

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.storage.artifacts import digest
from tests.analysis.fixtures import browser_fixture
from tests.support.benchmark_release import reference
from tests.support.daily_release import fixed_release as fixed_release
from tests.support.result_history import seed_python


def retained_result(days, frequency):
    result = browser_fixture(sessions=30)["result"]
    old = result["preparation"]["trading_dates"]
    # Re-date a constructed ledger, never a production result.
    raw = json.dumps(result)
    for i, day in enumerate(old):
        raw = raw.replace(day, f"DAY_{i:03d}")
    for i, day in enumerate(days):
        raw = raw.replace(f"DAY_{i:03d}", day)
    result = json.loads(raw)
    result["native"]["preflight"]["frequency"] = frequency
    result["native"]["benchmark"] = {"status": "not_selected"}
    if frequency == "1m":
        expanded = []
        for row in result["native"]["accounts"]:
            expanded.extend([{**row, "at": row["at"][:10] + "T09:31:00+08:00"}, row])
        result["native"]["accounts"] = expanded
    return result


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("gap", [None, "base", "middle", "no_pin"])
def test_comparison_uses_frozen_publications_and_never_rewrites_history(tmp_path, fixed_release, frequency, gap):
    market, calendar = fixed_release
    days = calendar[20:50]
    missing = [calendar[19]] if gap == "base" else [days[5]] if gap == "middle" else []
    pin = reference(tmp_path, [calendar[19], *days], missing)
    result = retained_result(days, frequency)
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    session = {"release": market.receipt()}
    if gap != "no_pin":
        session["benchmark_release"] = pin
    root = seed_python(app, result, session_fields=session)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    row = app.state.history.repo.row(result["job_id"])
    path = f"/api/v1/history/jobs/{result['job_id']}/result/analysis"
    with TestClient(app) as client:
        legacy = client.get(path).json()
        assert legacy["calculation_version"] == "returns-v1"
        assert "comparison" not in legacy and "return_difference" not in legacy
        values = []
        for family in ("python", "history"):
            for tail in ("analysis", "result/analysis"):
                response = client.get(f"/api/v1/{family}/jobs/{result['job_id']}/{tail}",
                                      params={"analysis_version": "returns-v2", "benchmark": "000300.XSHG"})
                assert response.status_code == 200, response.text
                values.append(response.json())
        assert all(value == values[0] for value in values)
        analysis = values[0]
        assert analysis["source_sha256"] == digest(result)
        assert analysis["comparison"]["selection"] == "000300.XSHG"
        assert analysis["comparison"]["source"] == "derived"
        assert analysis["sample_count"] == len(days)
        assert analysis["metrics"]["net_return"] == legacy["metrics"]["net_return"]
        if gap is None:
            base_return = .30
            relative = (1 + result["summary"]["net_return"]) / 1.30 - 1
            assert analysis["metrics"]["benchmark_return"]["value"] == pytest.approx(base_return)
            assert analysis["metrics"]["excess_return"]["value"] == pytest.approx(relative)
            assert analysis["series"][-1]["excess_return"] == pytest.approx(relative)
            assert {key: analysis["benchmark"]["release"][key] for key in pin} == pin
            assert {v["value"] for v in analysis["comparison"]["options"]} == {"frozen", "none", "000300.XSHG"}
        else:
            reason = {"base": "BENCHMARK_BASE_MISSING", "middle": "BENCHMARK_DAILY_GAP",
                      "no_pin": "BENCHMARK_SECURITY_UNAVAILABLE"}[gap]
            assert analysis["metrics"]["benchmark_return"]["reason"] == reason
            assert analysis["metrics"]["excess_return"]["value"] is None
            assert all(point["excess_return"] is None for point in analysis["series"])
        frozen = client.get(path, params={"analysis_version": "returns-v2"}).json()
        assert frozen["metrics"]["benchmark_return"]["reason"] == "missing_benchmark"
        disabled = client.get(path, params={"analysis_version": "returns-v2", "benchmark": "none"}).json()
        assert disabled["metrics"]["benchmark_return"]["reason"] == "benchmark_disabled"
        assert disabled["analysis_id"] != frozen["analysis_id"] != analysis["analysis_id"]
        assert client.get(path).json() == legacy
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert row == app.state.history.repo.row(result["job_id"])


@pytest.mark.parametrize("query", ["benchmark=000300.XSHG", "analysis_version=returns-v3", "analysis_version=returns-v2&benchmark=latest",
                                  "analysis_version=returns-v2&benchmark=none&benchmark=frozen", "analysis_version=returns-v2&source=latest"])
def test_analysis_rejects_ambiguous_or_unsupported_queries(tmp_path, query):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = browser_fixture()["result"]
    seed_python(app, result)
    with TestClient(app) as client:
        for family in ("python", "history"):
            response = client.get(f"/api/v1/{family}/jobs/{result['job_id']}/analysis?{query}")
            assert response.status_code == 422, response.text


@pytest.mark.parametrize("broken", ["missing", "integrity"])
def test_unavailable_pinned_reference_never_falls_back_to_another_release(tmp_path, fixed_release, broken):
    market, calendar = fixed_release
    days = calendar[20:50]
    pin = reference(tmp_path, [calendar[19], *days], [])
    pin = {**pin, **({"root": str(tmp_path / "missing-publication")} if broken == "missing"
                    else {"manifest_sha256": "0" * 64})}
    result = retained_result(days, "1d")
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    seed_python(app, result, session_fields={"release": market.receipt(), "benchmark_release": pin})
    with TestClient(app) as client:
        response = client.get(f"/api/v1/history/jobs/{result['job_id']}/analysis",
                              params={"analysis_version": "returns-v2", "benchmark": "000300.XSHG"})
    assert response.status_code == 200, response.text
    a = response.json()
    reason = "BENCHMARK_SOURCE_UNAVAILABLE" if broken == "missing" else "BENCHMARK_SOURCE_INVALID"
    assert a["metrics"]["excess_return"]["reason"] == reason
    assert a["metrics"]["net_return"]["value"] == result["summary"]["net_return"]
    assert a["comparison"]["reference_reason"] == reason
