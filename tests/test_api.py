import json
import socket
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from ashare_quant import __version__
from ashare_quant.api import create_app
from ashare_quant.worker import work_one


@pytest.fixture
def client(store):
    with TestClient(create_app(str(store.path))) as client:
        yield client


def test_versioned_api_queue_to_read_only_execution_projection(client, store):
    version = client.post("/api/v1/strategies", json={"name": "Hold", "scenario": "buy_hold"}).json()
    payload = {"strategy_version_id": version["version_id"], "idempotency_key": "http-demo"}
    response = client.post("/api/v1/runs", json=payload)
    assert response.status_code == 202
    rid = response.json()["run_id"]
    assert client.post("/api/v1/runs", json=payload).json()["run_id"] == rid
    assert client.get(f"/api/v1/runs/{rid}/account").json() is None
    work_one(store, rid)
    assert client.get(f"/api/v1/runs/{rid}").json()["status"] == "succeeded"
    assert client.get(f"/api/v1/runs/{rid}/account").json()["cash"] == "9000.00"
    assert len(client.get(f"/api/v1/runs/{rid}/orders").json()) == 1
    assert len(client.get(f"/api/v1/runs/{rid}/fills").json()) == 1
    assert client.put(f"/api/v1/runs/{rid}/account", json={"cash": "0"}).status_code == 405
    logs = client.get(f"/api/v1/runs/{rid}/logs").json()
    assert client.get(f"/api/v1/runs/{rid}/logs?after={logs[-1]['index']}").json() == []
    assert client.get(f"/api/v1/runs/{rid}/manifest").json()["real_strategy_executed"] is False


def test_unsupported_data_and_real_engine_fail_without_network(client, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Backend attempted network activity")
    monkeypatch.setattr(socket, "create_connection", no_network)
    version = client.post("/api/v1/strategies", json={"name": "Hold", "scenario": "buy_hold"}).json()
    for mode in ("research", "paper"):
        r = client.post("/api/v1/runs", json={"strategy_version_id": version["version_id"],
            "idempotency_key": mode, "mode": mode, "engine_id": "rqalpha"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "ENGINE_NOT_ADMITTED"
    r = client.post("/api/v1/data/admission", json={
        "dataset_id": "unaccepted", "securities": ["600000.XSHG"], "frequency": "daily",
        "start": "2026-09-28T00:00:00+08:00", "end": "2026-09-28T15:00:00+08:00",
        "as_of": "2026-09-28T15:00:00+08:00", "visibility": "verified",
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "ENGINE_DATA_CAPABILITIES_MISSING"
    assert client.get("/api/v1/runs").json() == []


def test_api_errors_schema_and_host_policy(client):
    assert client.get("/api/v1/runs/missing").status_code == 404
    assert client.get("/api/v1/health", headers={"host": "public.example"}).status_code == 400
    assert client.post("/api/v1/strategies", json={"name": "Hold", "scenario": "buy_hold",
        "code": "arbitrary user code"}).status_code == 422
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["info"]["version"] == __version__
    assert schema["components"]["schemas"]["Account"]["properties"]["cash"]["type"] == "string"
    assert schema["components"]["schemas"]["DataQuery"]["properties"]["as_of"]["format"] == "date-time"
    capability = client.get("/api/v1/capabilities").json()
    assert "fixture" in capability["executable_modes"]
    assert capability["rqalpha_validation"] in (
        "optional_dependency_missing", "optional_native_installed_use_scoped_m2_catalogue")
    assert capability["real_market_validation"] == "not_run"


def test_cli_roundtrip_and_refusal_exit_code(tmp_path):
    base = [sys.executable, "-m", "ashare_quant.cli", "--db", str(tmp_path / "cli.sqlite3")]
    demo = subprocess.run(base + ["demo"], capture_output=True, text=True)
    assert demo.returncode == 0, demo.stdout + demo.stderr
    output = json.loads(demo.stdout)
    assert output["state"]["account"]["cash"] == "9000.00"
    version_id = output["state"]["run"]["strategy_version_id"]
    rejected = subprocess.run(base + ["run-create", "--strategy", version_id, "--key", "real",
        "--mode", "paper", "--engine", "rqalpha"], capture_output=True, text=True)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error"]["code"] == "ENGINE_NOT_ADMITTED"
