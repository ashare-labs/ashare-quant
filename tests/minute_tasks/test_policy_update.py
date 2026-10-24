"""Runtime budget updates never rewrite accepted tasks or block verified history."""

import json

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.storage.artifacts import canonical, digest
from ashare_quant.tasks.executor import Configuration
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.worker import work_one
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import fixed_release as fixed_release


def test_environment_update_keeps_database_and_read_only_history(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    first = create_app(str(path), diagnostics_configuration={})
    original = path.read_bytes()
    monkeypatch.setenv("ASHARE_QUANT_JOB_POLICY", json.dumps({"execution_seconds": 180}))
    changed = create_app(str(path), diagnostics_configuration={})
    with TestClient(changed) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/history/jobs").json() == {"items": []}
        status = client.get("/api/v1/tasks/service").json()
        assert status["policy"]["execution_seconds"] == 180
        assert status["execution_policy_scope"] == "frozen_per_task"
    assert first.state.tasks.repo.policy.execution_seconds == 120
    assert changed.state.tasks.repo.initial_policy.execution_seconds == 120
    assert path.read_bytes() == original


def test_queued_and_running_tasks_keep_frozen_policy_new_tasks_get_override(daily_jobs, monkeypatch):
    app, original, body = daily_jobs
    body.update(source="def initialize(context): pass", end_date=body["start_date"])
    old = original.create(PythonCreate.model_validate(body))
    old_root = original.directory(old.job_id)
    frozen = {name: (old_root / name).read_bytes() for name in
              ("accepted-manifest.json", "session.json", "submission.json", "strategy.py")}
    changed_app = create_app(str(original.repo.store.path), diagnostics_configuration={},
                             daily_tasks_configuration=Configuration(None, ServicePolicy(execution_seconds=180)))
    changed = changed_app.state.python_jobs
    changed.product = original.product
    monkeypatch.setattr(changed, "environment", lambda: "b" * 64)
    observed, new = [], []

    def stage(name, service, row, process):
        if name == "spawned":
            observed.append(service.executor.policy.execution_seconds)
            new.append(changed.create(PythonCreate.model_validate(body | {
                "environment_id": "b" * 64, "idempotency_key": "new-budget"})))
            assert service.repo.policy.execution_seconds == 120

    work_one(changed, old.job_id, _stage=stage)
    assert observed == [120] and changed.get(old.job_id).status == "succeeded"
    assert frozen == {name: (old_root / name).read_bytes() for name in frozen}
    manifest = changed.check(changed.repo.row(new[0].job_id))
    assert manifest.session["resource_policy"]["execution_seconds"] == 180
    work_one(changed, new[0].job_id,
             _stage=lambda name, service, *_: observed.append(service.executor.policy.execution_seconds)
             if name == "spawned" else None)
    assert observed == [120, 180] and changed.get(new[0].job_id).status == "succeeded"
    logs = original.repo.logs(old.job_id)
    claimed = next(row for row in logs if row["event"] == "writer_claimed")
    assert claimed["details"]["service_policy_sha256"] == digest(original.repo.policy)
    with TestClient(changed_app) as client:
        assert client.get("/api/v1/history/jobs/" + old.job_id + "/result").status_code == 200


def test_corrupted_initial_policy_hash_still_refuses(tmp_path):
    path = tmp_path / "control.sqlite3"
    app = create_app(str(path), diagnostics_configuration={})
    with app.state.store.connection(transaction=True) as db:
        db.execute("UPDATE task_service SET policy_sha256=?", ("0" * 64,))
    original = path.read_bytes()
    changed = create_app(str(path), diagnostics_configuration={},
                         daily_tasks_configuration=Configuration(None, ServicePolicy(execution_seconds=180)))
    with TestClient(changed) as client:
        assert client.get("/api/v1/health").json()["error"]["code"] == "M2_JOB_POLICY_CHANGED"
    assert path.read_bytes() == original


def test_old_database_with_retired_phase_fields_remains_byte_identical(tmp_path):
    path = tmp_path / "control.sqlite3"
    app = create_app(str(path), diagnostics_configuration={})
    legacy = {**app.state.tasks.repo.policy.model_dump(), "callback_seconds": 5, "module_load_seconds": 5}
    with app.state.store.connection(transaction=True) as db:
        db.execute("UPDATE task_service SET policy=?,policy_sha256=?",
                   (canonical(legacy).decode(), digest(legacy)))
    original = path.read_bytes()
    changed = create_app(str(path), diagnostics_configuration={},
                         daily_tasks_configuration=Configuration(None, ServicePolicy(execution_seconds=180)))
    with TestClient(changed) as client:
        assert client.get("/api/v1/health").status_code == 200
        status = client.get("/api/v1/tasks/service").json()
        assert status["initial_policy_sha256"] == digest(legacy)
        assert status["policy"]["execution_seconds"] == 180
        assert "callback_seconds" not in status["policy"]
    assert changed.state.tasks.execution_policy({"session": {}}).execution_seconds == 120
    assert path.read_bytes() == original


def test_old_environment_json_does_not_reintroduce_a_startup_gate(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    create_app(str(path), diagnostics_configuration={})
    before = path.read_bytes()
    monkeypatch.setenv("ASHARE_QUANT_JOB_POLICY", json.dumps({
        "execution_seconds": 180, "module_load_seconds": 1, "callback_seconds": 1}))
    with pytest.warns(UserWarning, match="Ignored retired"):
        app = create_app(str(path), diagnostics_configuration={})
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        policy = client.get("/api/v1/tasks/service").json()["policy"]
        assert policy["execution_seconds"] == 180
        assert not {"module_load_seconds", "callback_seconds"} & policy.keys()
        assert client.get("/api/v1/history/jobs").status_code == 200
    assert path.read_bytes() == before
