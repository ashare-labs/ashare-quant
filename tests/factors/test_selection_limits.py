"""No count/year gate: run full selected panels through the actual task API."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.storage.artifacts import atomic, sha
from ashare_quant.tasks.factors.contracts import EvaluationOptions
from ashare_quant.tasks.worker import work_one
from tests.support.daily_release import fixed_release as fixed_release


def panel_app(tmp_path, count, dates, *, ranked=False):
    ids = [f"{600000+i}.XSHG" for i in range(count)]
    closes = [[10+i+n/100 for n in range(count)] for i in range(len(dates))]
    fields = {"close": closes}
    if ranked:
        values = [[10+n/100 + i*(n % 3-1)*.001 for n in range(count)] for i in range(len(dates))]
        fields.update(open=values, high=values)
    payload = dict(schema="alpha191.panel.v1", sessions=dates, securities=ids,
                   universe=[[True]*count for _ in dates], fields=fields, adjustment="raw",
                   units={"price": "CNY/share", "volume": "share", "amount": "CNY"})
    path = tmp_path / "panel.json"
    atomic(path, payload)
    app = create_app(str(tmp_path / "control.sqlite3"), factor_sources={
        "fixture": dict(kind="alpha191", path=str(path), sha256=sha(path.read_bytes()))})
    body = dict(environment_id=app.state.factor_jobs.environment(), idempotency_key="full-scope",
                name="Full selected panel", factor_id="alpha191:alpha006" if ranked else "alpha191:alpha014",
                definition="gtja191.explicit.v1", source_id="fixture", securities=ids,
                history_start=dates[0], start_date=dates[0], end_date=dates[-1])
    return app, body


def run(client, app, body):
    response = client.post("/api/v1/factors/jobs", json=body)
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    assert work_one(app.state.factor_jobs, job_id) == job_id
    job = client.get(f"/api/v1/factors/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    response = client.get(f"/api/v1/factors/jobs/{job_id}/result")
    assert response.status_code == 200, response.text
    result = response.json()
    assert client.get(f"/api/v1/history/jobs/{job_id}/result").json() == result
    return result


@pytest.mark.parametrize("count", [101, 300, 5000])
def test_full_stock_selection_runs_without_count_gate(tmp_path, count):
    dates = pd.bdate_range("2024-01-02", periods=12).strftime("%Y-%m-%d").tolist()
    app, body = panel_app(tmp_path, count, dates)
    with TestClient(app) as client:
        result = run(client, app, body)
        assert result["summary"]["valid_cells"] == count*7
        assert len(result["rows"]) == count*12
        assert {round(r["value"], 8) for r in result["rows"] if r["value"] is not None} == {5.0}
        for selection in ([], [body["securities"][0]]*2):
            assert client.post("/api/v1/factors/jobs", json=body | {"securities": selection}).status_code == 422
        unknown = client.post("/api/v1/factors/jobs", json=body | {
            "securities": ["000999.XSHE"], "idempotency_key": "outside"})
        assert unknown.status_code == 409 and unknown.json()["error"]["code"] == "FACTOR_SECURITY_SCOPE"


def test_rank_uses_entire_300_asset_cross_section(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=12).strftime("%Y-%m-%d").tolist()
    app, body = panel_app(tmp_path, 300, dates, ranked=True)
    with TestClient(app) as client:
        result = run(client, app, body)
    expected = -pd.Series([i % 3-1 for i in range(300)]).rank(pct=True)
    final = {r["security"]: r["value"] for r in result["rows"] if r["date"] == dates[-1]}
    for sid, value in zip(body["securities"], expected):
        assert final[sid] == pytest.approx(value)


def test_eleven_year_scope_runs_and_inverted_dates_refuse(tmp_path):
    dates = pd.bdate_range("2013-01-02", "2024-01-02").strftime("%Y-%m-%d").tolist()
    app, body = panel_app(tmp_path, 1, dates)
    with TestClient(app) as client:
        result = run(client, app, body)
        assert result["summary"]["valid_cells"] == len(dates)-5
        assert len(result["rows"]) == len(dates)
        assert client.post("/api/v1/factors/jobs", json=body | {"history_start": "2025-01-01"}).status_code == 422


def test_full_panel_above_retired_cell_ceiling_and_evaluation_minimum(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=21).strftime("%Y-%m-%d").tolist()
    app, body = panel_app(tmp_path, 5000, dates)
    with TestClient(app) as client:
        result = run(client, app, body)
        assert len(result["rows"]) == 105000
        assert result["summary"]["valid_cells"] == 80000
    assert EvaluationOptions(min_assets=100001).min_assets == 100001
    for value in (1, True, 2.5):
        with pytest.raises(ValidationError):
            EvaluationOptions(min_assets=value)


@pytest.mark.parametrize("fixed_release", [{"securities": [f"{600000+i}.XSHG" for i in range(300)],
                                           "calendar_days": 20}], indirect=True)
def test_released_owner_returns_full_selection_for_factor_inputs(fixed_release):
    from ashare_data.product.market_release import ReleasedDailyProduct

    from ashare_quant.factors.inputs import market_panel
    from ashare_quant.tasks.factors.contracts import FactorCreate

    release, dates = fixed_release
    spec = dict(market_release=dict(root=str(release.root), release_id=release.release_id,
                                   manifest_sha256=release.sha256))
    request = FactorCreate(environment_id="a"*64, idempotency_key="scope", name="scope",
                           factor_id="alpha191:alpha014", definition="gtja191.explicit.v1",
                           source_id="market", securities=list(release.identifiers),
                           history_start=dates[0], start_date=dates[0], end_date=dates[-1])
    panel, _, _ = market_panel(ReleasedDailyProduct(spec), spec, request)
    assert len(panel["securities"]) == 300 and panel["sessions"] == dates
    assert all(len(row) == 300 for row in panel["fields"]["close"])
