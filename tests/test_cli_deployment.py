"""Legacy commands cannot accidentally select a second working-directory database."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.cli import main
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize
from ashare_quant.tasks.daily.executor import Configuration


@pytest.fixture
def deployment(tmp_path):
    from ashare_data.storage.local_daily import LocalDailyStore

    root = tmp_path / "selected"
    root.mkdir()
    path = root / "deployment.json"
    path.write_text(json.dumps({
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "market", "role": "market"},
            "releases": {"kind": "store", "path": "releases", "role": "publication"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
            "control_db": "state/control.sqlite3", "paper_root": "state/paper",
            "api_port": 18849, "paper_port": 18848, "web_port": 41849,
        }},
    }))
    config = resolve(path)
    LocalDailyStore(config.daily_root).close()
    initialize(config)
    return config


def legacy(tmp_path, args, **variables):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASHARE_")}
    env.update(variables, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-B", "-m", "ashare_quant.cli", *args],
                          cwd=tmp_path, env=env, text=True, capture_output=True, timeout=90)


@pytest.mark.parametrize("command", [["task-worker", "--once"], ["worker", "--once"], ["tasks"]])
def test_ambiguous_state_refused_without_creating_database(tmp_path, command):
    run = legacy(tmp_path, command)
    assert run.returncode == 2, run.stderr
    assert json.loads(run.stdout)["error"]["code"] == "CLI_STATE_NOT_CONFIGURED"
    assert not (tmp_path / ".state").exists()


def test_invalid_deployment_cannot_fall_back_to_db(tmp_path):
    wrong = tmp_path / "wrong.sqlite3"
    run = legacy(tmp_path, ["task-worker", "--once"],
                 ASHARE_DEPLOYMENT=str(tmp_path / "missing.json"), ASHARE_QUANT_DB=str(wrong))
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "DEPLOYMENT_INVALID"
    assert not wrong.exists() and not (tmp_path / ".state").exists()


@pytest.mark.parametrize("selection", ["deployment_env", "data_root_env", "deployment_flag"])
def test_worker_uses_same_manifest_state_and_ignores_stale_db_env(deployment, tmp_path, selection):
    variables = {"ASHARE_QUANT_DB": str(tmp_path / "stale.sqlite3")}
    arguments = ["task-worker", "--once"]
    if selection == "deployment_env":
        variables["ASHARE_DEPLOYMENT"] = str(deployment.deployment)
    elif selection == "data_root_env":
        variables["ASHARE_DATA_ROOT"] = str(deployment.data_root)
    else:
        arguments = ["--deployment", str(deployment.deployment), *arguments]
    run = legacy(tmp_path, arguments, **variables)
    assert run.returncode == 0, run.stdout + run.stderr
    reply = json.loads(run.stdout)
    assert reply["control_db"] == str(deployment.control_db)
    assert reply["processed_job_id"] is None
    assert not (tmp_path / "stale.sqlite3").exists() and not (tmp_path / ".state").exists()


def test_explicit_database_conflict_refused_before_writing(deployment, tmp_path):
    wrong = tmp_path / "other" / "control.sqlite3"
    run = legacy(tmp_path, ["--db", str(wrong), "task-worker", "--once"],
                 ASHARE_DEPLOYMENT=str(deployment.deployment))
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "PRODUCT_STATE_MISMATCH"
    assert not wrong.parent.exists()


def test_serve_uses_selected_deployment_and_port(deployment, monkeypatch, capsys):
    import ashare_quant.deployment.runtime as runtime

    calls = []
    monkeypatch.setattr(runtime, "run", lambda config, component, **options: calls.append(
        (config, component, options)) or 0)
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(deployment.deployment))
    assert main(["serve"]) == 0
    assert calls == [(deployment, "api", {"once": False, "task_id": None})]
    assert main(["serve", "--port", "18850"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "DEPLOYMENT_PORT_MISMATCH"
    assert len(calls) == 1


def test_unselected_explicit_db_remains_available(tmp_path):
    path = tmp_path / "chosen.sqlite3"
    run = legacy(tmp_path, ["--db", str(path), "init"])
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["database"] == str(path)
    assert path.exists() and not (tmp_path / ".state").exists()


@pytest.mark.parametrize("selection", ["explicit_deployment", "explicit_profile"])
def test_openapi_preserves_explicit_selection(deployment, tmp_path, selection):
    if selection == "explicit_deployment":
        arguments = ["--deployment", str(deployment.deployment), "openapi"]
        variables = {"ASHARE_DEPLOYMENT": str(tmp_path / "missing.json")}
    else:
        document = json.loads(deployment.deployment.read_text())
        document["profiles"]["evaluation"] = {
            **document["profiles"].pop("product"), "kind": "evaluation",
        }
        deployment.deployment.write_text(json.dumps(document))
        arguments = ["--profile", "evaluation", "openapi"]
        variables = {"ASHARE_DEPLOYMENT": str(deployment.deployment)}
    run = legacy(tmp_path, arguments, **variables)
    assert run.returncode == 0, run.stdout + run.stderr
    paths = json.loads(run.stdout)["paths"]
    assert "/{path}" not in paths and "/api/v1/product/config" in paths
    assert not (tmp_path / ".state").exists()


def test_legacy_worker_runs_api_task_from_selected_deployment(deployment, tmp_path):
    source = os.getenv("ASHARE_QUANT_TEST_CORPORATE_INPUT")
    if not source:
        pytest.skip("Caller-supplied frozen corporate input required for real worker regression")
    app = create_app(str(deployment.control_db), product_configuration=deployment,
                     daily_tasks_configuration=Configuration(deployment.owner_store_root))
    dataset = app.state.portfolio_jobs.import_data(Path(source), "CLI recovery regression")
    prefix = "/api/v1/portfolio"
    with TestClient(app) as client:
        selection = dict(dataset_id=dataset.dataset_id, securities=["000651.XSHE"],
                         start_date="2022-04-06", end_date="2022-04-11", accept_retrospective_state=True)
        preflight = client.post(prefix + "/preflight", json=selection).json()
        assert preflight["ready"], preflight
        strategy = client.post(prefix + "/strategies", json={"algorithm": "equal-weight"}).json()
        response = client.post(prefix + "/jobs", json=selection | dict(
            name="Selected legacy worker", idempotency_key="legacy-shared-state",
            strategy_version_id=strategy["version_id"], assumption_ack_sha256=preflight["assumption_ack_sha256"]))
        assert response.status_code == 201, response.text
        job = response.json()
        run = legacy(tmp_path, ["task-worker", "--once"], ASHARE_DEPLOYMENT=str(deployment.deployment))
        assert run.returncode == 0, run.stdout + run.stderr
        reply = json.loads(run.stdout.strip().splitlines()[-1])
        assert reply["processed_job_id"] == job["job_id"]
        assert reply["control_db"] == str(deployment.control_db)
        status = client.get(prefix + "/jobs/" + job["job_id"]).json()
        assert status == reply["job"] and status["status"] == "succeeded"
        result = client.get(prefix + "/jobs/" + job["job_id"] + "/result").json()
        assert result["audit"]["passed"] and result["trades"]
        assert result["receipt"]["corporate_entitlements"]["000651.XSHE"]
    assert not (tmp_path / ".state").exists()
