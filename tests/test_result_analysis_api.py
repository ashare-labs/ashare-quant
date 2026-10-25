"""Frozen storage -> verified result -> one analysis; no worker or market I/O."""

import copy

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.storage.artifacts import atomic, canonical, digest
from tests.analysis.fixtures import browser_fixture
from tests.test_formal_backend import seed_history


def seed_python(app, result, *, request_fields=None, session_fields=None):
    job_id = result["job_id"]
    session = {"run_id": "synthetic-frozen-input"}
    session.update(session_fields or {})
    request = {"name": "frozen analysis fixture", "idempotency_key": "analysis-test"}
    request.update(request_fields or {})
    strategy = b"raise AssertionError('historical analysis must not execute this source')\n"
    from ashare_quant.storage.artifacts import sha

    manifest = {
        "schema_version": "python-demand-job.v2", "job_id": job_id, "request": request,
        "session": session, "session_sha256": digest(session), "session_file_sha256": digest(session),
        "service_identity": {"source_sha256": "b" * 64}, "files": {"strategy.py": sha(strategy)},
    }
    root = app.state.history.directory(job_id)
    root.mkdir()
    for name, value in (("accepted-manifest.json", manifest), ("session.json", session),
                        ("published.json", result), ("strategy.py", strategy)):
        atomic(root / name, value)
    reference = {"result_id": job_id, "href": "/retained-result", "snapshot_sha256": digest(result),
                 "evidence_manifest_sha256": digest(manifest), "publication": "publication-1"}
    with app.state.store.connection(transaction=True) as db:
        db.execute("INSERT INTO tasks(job_id,idempotency_key,request_hash,request,manifest,manifest_sha256,account_id,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, "analysis-test", digest(request), canonical(request).decode(), canonical(manifest).decode(),
                    digest(manifest), "bt-" + "a" * 32, "succeeded", "2025-01-01T00:00:00Z",
                    "2025-01-01T00:00:00Z", canonical(reference).decode()))
    return root


def frozen_fixture():
    result = browser_fixture()["result"]
    days = result["preparation"]["trading_dates"]
    result["native"]["benchmark"] = {
        "schema": "aq-benchmark-prices.v1", "security": "000300.XSHG", "status": "available",
        "price_basis": "published_close_price_return", "base": {"date": "2024-12-31", "close": 1000},
        "prices": [{"date": day, "close": 1000 + i + 1} for i, day in enumerate(days)],
        "receipt": {"receipt_sha256": "a" * 64},
    }
    return result


def test_analysis_reads_verified_history_and_aliases_without_side_effects(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    root = seed_python(app, result)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    row = app.state.history.repo.row(result["job_id"])
    def no_market(*args):
        raise AssertionError("analysis must not fetch current market data")
    monkeypatch.setattr(app.state.product, "call", no_market)
    with TestClient(app) as client:
        values = []
        for prefix in ("python", "history"):
            for tail in ("analysis", "result/analysis"):
                response = client.get(f"/api/v1/{prefix}/jobs/{result['job_id']}/{tail}")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                values.append(response.json())
        assert all(v == values[0] for v in values)
        analysis = values[0]
        assert analysis["source_sha256"] == digest(result)
        assert analysis["benchmark"]["source_sha256"] == digest(result["native"]["benchmark"])
        assert analysis["metrics"]["benchmark_return"]["value"] == pytest.approx(.07)
        assert analysis["metrics"]["net_return"]["value"] == result["summary"]["net_return"]
        assert analysis["trading"]["status"] == "ok"
        assert analysis["sample_count"] == 70
        assert client.get(f"/api/v1/history/jobs/{result['job_id']}/result").json() == result
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert row == app.state.history.repo.row(result["job_id"])


@pytest.mark.parametrize("target,code", [("published.json", "HISTORY_RESULT_CONFLICT"),
                                        ("strategy.py", "HISTORY_EVIDENCE_CONFLICT")])
def test_analysis_rejects_tampered_frozen_evidence(tmp_path, target, code):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    root = seed_python(app, result)
    (root / target).write_bytes(b"{}")
    with TestClient(app) as client:
        response = client.get(f"/api/v1/history/jobs/{result['job_id']}/result/analysis")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("benchmark", [None, {"schema": "aq-benchmark-prices.v1", "status": "unavailable",
                                           "security": "000300.XSHG", "reason": "BENCHMARK_DAILY_GAP",
                                           "missing_dates": ["2025-01-02"]}])
def test_absent_benchmark_keeps_strategy_metrics_and_exact_gap(tmp_path, benchmark):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = frozen_fixture()
    if benchmark is None:
        result["native"].pop("benchmark")
    else:
        result["native"]["benchmark"] = benchmark
    seed_python(app, result)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/history/jobs/{result['job_id']}/result/analysis")
    assert response.status_code == 200, response.text
    analysis = response.json()
    assert analysis["metrics"]["net_return"]["status"] == "ok"
    assert analysis["metrics"]["benchmark_return"]["value"] is None
    reason = "BENCHMARK_DAILY_GAP" if benchmark else "missing_benchmark"
    assert analysis["metrics"]["benchmark_return"]["reason"] == reason
    assert reason in analysis["quality"]["reasons"]
    if benchmark:
        assert analysis["benchmark"]["missing_dates"] == benchmark["missing_dates"]


def test_invalid_account_analysis_returns_structured_error(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    result = copy.deepcopy(frozen_fixture())
    result["summary"]["net_return"] = 1
    seed_python(app, result)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/history/jobs/{result['job_id']}/result/analysis")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "inconsistent_frozen_summary"


def test_retired_result_still_readable_without_invented_analysis(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    job_id, _, expected = seed_history(app, "daily")
    with TestClient(app) as client:
        assert client.get(f"/api/v1/history/jobs/{job_id}/result").json() == expected
        response = client.get(f"/api/v1/history/jobs/{job_id}/result/analysis")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RESULT_ANALYSIS_UNSUPPORTED"
