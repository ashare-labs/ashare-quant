import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from ashare_quant import __version__
from ashare_quant.api import create_app


@pytest.fixture
def client(store):
    with TestClient(create_app(str(store.path))) as client:
        yield client


def test_api_errors_schema_and_host_policy(client):
    assert client.get("/api/v1/runs/missing").status_code == 404
    assert client.get("/api/v1/health", headers={"host": "public.example"}).status_code == 400
    assert client.post("/api/v1/strategies", json={"name": "Hold", "scenario": "buy_hold",
        "code": "arbitrary user code"}).status_code in {404, 405}
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["info"]["version"] == __version__
    assert schema["components"]["schemas"]["Account"]["properties"]["cash"]["type"] == "string"
    assert "ErrorResponse" in schema["components"]["schemas"]
    capability = client.get("/api/v1/capabilities").json()
    assert "fixture" not in capability["executable_modes"]
    assert capability["rqalpha_validation"] in (
        "optional_dependency_missing", "optional_native_installed_use_task_catalogue")
    assert capability["real_market_validation"] == "not_run"


def test_retired_cli_commands_are_not_installed(tmp_path):
    base = [sys.executable, "-m", "ashare_quant.cli", "--db", str(tmp_path / "cli.sqlite3")]
    for command in ("demo", "scenarios", "strategy-create", "run-create", "portfolio-create", "task-data-import"):
        response = subprocess.run(base + [command], capture_output=True, text=True)
        assert response.returncode == 2 and "invalid choice" in response.stderr
        assert not (tmp_path / "cli.sqlite3").exists()
