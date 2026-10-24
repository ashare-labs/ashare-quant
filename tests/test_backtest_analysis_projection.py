"""List metrics come from the same verified analysis, without mutating old results."""

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from tests.test_result_analysis_api import frozen_fixture, seed_python


def seed_listed_result(app, result):
    days = result["preparation"]["trading_dates"]
    return seed_python(app, result, request_fields={
        "strategy_id": "research/example.py", "start_date": days[0], "end_date": days[-1],
        "frequency": "1m", "initial_cash": result["summary"]["initial_cash"],
    })


def listed(client):
    response = client.get("/api/v1/python/backtests", params={"strategy_id": "research/example.py"})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


@pytest.mark.parametrize("benchmark", ["available", "flat", "not_selected", "gap"])
def test_list_matches_all_analysis_metrics_without_replaying_or_market_reads(tmp_path, monkeypatch, benchmark):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    if benchmark == "flat":
        for point in result["native"]["benchmark"]["prices"]:
            point["close"] = 1000
    elif benchmark == "not_selected":
        result["native"]["benchmark"] = {"status": "not_selected"}
    elif benchmark == "gap":
        result["native"]["benchmark"] = {
            "schema": "aq-benchmark-prices.v1", "status": "unavailable", "security": "000300.XSHG",
            "reason": "BENCHMARK_DAILY_GAP", "missing_dates": ["2025-01-02"],
        }
    root = seed_listed_result(app, result)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    original_row = app.state.history.repo.row(result["job_id"])

    def no_market(*args):
        raise AssertionError("A historical list must not fetch current market data")

    monkeypatch.setattr(app.state.product, "call", no_market)
    reads = []
    read_result = app.state.history.result

    def counted_result(job_id):
        reads.append(job_id)
        return read_result(job_id)

    monkeypatch.setattr(app.state.history, "result", counted_result)
    with TestClient(app) as client:
        item = listed(client)
        assert reads == [result["job_id"]]  # Don't reload a second result for analysis.
        response = client.get(f"/api/v1/history/jobs/{result['job_id']}/analysis")
        assert response.status_code == 200, response.text
        analysis = response.json()
        assert item["result_available"] and item["result_error"] is None and item["analysis_error"] is None
        assert item["result"]["snapshot_sha256"] == analysis["source_sha256"]
        for key, metric in analysis["metrics"].items():
            assert item["summary"][key] == metric["value"]
        for key, value in result["summary"].items():
            assert item["summary"][key] == value
        expected = .07 if benchmark == "available" else 0 if benchmark == "flat" else None
        if expected is None:
            assert item["summary"]["benchmark_return"] is None
        else:
            assert item["summary"]["benchmark_return"] == pytest.approx(expected)
        assert listed(client) == item
        assert client.get(f"/api/v1/history/jobs/{result['job_id']}/result").json() == result
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert original_row == app.state.history.repo.row(result["job_id"])


def test_analysis_failure_keeps_verified_frozen_result_available(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    result["summary"]["net_return"] = 1  # Frozen but internally inconsistent account evidence.
    seed_listed_result(app, result)
    with TestClient(app) as client:
        item = listed(client)
        assert item["result_available"] and item["result_error"] is None
        assert item["summary"] == result["summary"]
        assert item["analysis_error"]["code"] == "RESULT_ANALYSIS_UNAVAILABLE"
        assert item["analysis_error"]["details"]["reason"] == "inconsistent_frozen_summary"
        assert client.get(f"/api/v1/history/jobs/{result['job_id']}/result").json() == result


def test_corrupt_result_is_not_projected_or_relabelled_as_analysis_failure(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    root = seed_listed_result(app, result)
    (root / "published.json").write_text("{}")
    with TestClient(app) as client:
        item = listed(client)
    assert not item["result_available"] and item["summary"] is None
    assert item["result_error"]["code"] == "HISTORY_RESULT_CONFLICT"
    assert item["analysis_error"] is None
