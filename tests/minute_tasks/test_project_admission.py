"""Complete project admission, identity and historical evidence on isolated storage."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, digest, sha
from ashare_quant.strategies.projects import ProjectFiles
from ashare_quant.tasks.minute.contracts import PythonCreate
from tests.minute_tasks.test_api import setup as setup


@pytest.fixture
def project_setup(setup, tmp_path):
    app, jobs, legacy = setup
    jobs.product.configured = lambda: SimpleNamespace(
        control_db=tmp_path / "control.sqlite3", document={"profiles": {"product": {}}}, profile="product",
    )
    files = ProjectFiles(tmp_path)
    files.files.mkdir("分类")
    saved = files.create("分类/项目")
    project = {
        "project_id": saved["strategy_id"], "entrypoint": "main.py",
        "files": {
            "main.py": "from .signals import choose\ndef initialize(context):\n    choose()\n",
            "signals.py": "def choose():\n    return 100\n",
            "unused/__init__.py": "",
            "unused/filters.py": "FLAG = True\n",
        },
    }
    body = {k: v for k, v in legacy.items() if k != "source"}
    body.update(strategy_id=saved["strategy_id"], project=project)
    return app, jobs, files, saved, body


def test_all_submitted_files_are_frozen_not_replaced_with_saved_sources(project_setup):
    app, jobs, files, saved, body = project_setup
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 201, response.text
        job = response.json()
        assert "source" not in job["request"]
        assert job["request"]["project"] == body["project"]
        root = jobs.directory(job["job_id"])
        manifest = json.loads((root / "accepted-manifest.json").read_bytes())
        assert manifest["schema_version"] == "python-project-job.v3"
        assert manifest["session"]["project_sha256"] == digest(body["project"])
        for name, source in body["project"]["files"].items():
            assert (root / "source" / name).read_text() == source
            assert manifest["files"]["source/" + name] == sha(source.encode())
        assert not (root / "strategy.py").exists()
        assert files.read(saved["path"]) == saved  # Running a draft never saves it.
        assert client.get("/api/v1/python/jobs").json()["items"][0]["job_id"] == job["job_id"]
        history = client.get("/api/v1/history/jobs/" + job["job_id"])
        assert history.status_code == 200, history.text
        assert history.json()["request"]["project"] == body["project"]
        runs = client.get("/api/v1/python/backtests", params={"strategy_id": saved["strategy_id"]}).json()
        assert runs["identity_model"] == "stable_project_id" and runs["total"] == 1


def test_workspace_project_admission_freezes_scope_and_rejects_cross_scope_retry(project_setup, tmp_path):
    app, jobs, _, _, body = project_setup
    namespace = "a" * 32
    files = ProjectFiles(tmp_path, workspace=namespace)
    saved = files.create("scoped")
    body["strategy_id"] = saved["strategy_id"]
    body["project"]["project_id"] = saved["strategy_id"]
    with TestClient(app) as client:
        # A project from another namespace cannot be submitted through the legacy root.
        assert client.post("/api/v1/python/jobs", json=body).status_code == 404
        response = client.post("/api/v1/python/jobs", json=body,
                               headers={"X-Quant-Workspace": namespace})
        assert response.status_code == 201, response.text
        identifier = response.json()["job_id"]
        assert jobs.check(jobs.repo.row(identifier)).session["strategy_workspace"] == namespace
        assert client.post("/api/v1/python/jobs", json=body,
                           headers={"X-Quant-Workspace": namespace}).json()["job_id"] == identifier
        for header in ({}, {"X-Quant-Workspace": "b" * 32}):
            refused = client.post("/api/v1/python/jobs", json=body, headers=header)
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "STRATEGY_WORKSPACE_CONFLICT"
        assert len(jobs.repo.rows()) == 1


def test_helper_change_changes_immutable_identity_and_repeated_click_is_idempotent(project_setup):
    _, jobs, _, _, body = project_setup
    request = PythonCreate.model_validate(body)
    with ThreadPoolExecutor(2) as pool:
        repeated = list(pool.map(lambda _: jobs.create(request), range(2)))
    assert repeated[0].job_id == repeated[1].job_id
    changed = json.loads(json.dumps(body))
    changed["project"]["files"]["signals.py"] += "NEW_VALUE = 200\n"
    with pytest.raises(DomainError) as conflict:
        jobs.create(PythonCreate.model_validate(changed))
    assert conflict.value.code == "M2_JOB_IDEMPOTENCY_CONFLICT"
    changed["idempotency_key"] = "changed-helper"
    second = jobs.create(PythonCreate.model_validate(changed))
    assert second.job_id != repeated[0].job_id
    first_manifest = jobs.check(jobs.repo.row(repeated[0].job_id))
    second_manifest = jobs.check(jobs.repo.row(second.job_id))
    assert first_manifest.session["project_sha256"] != second_manifest.session["project_sha256"]
    assert first_manifest.request.project.files["signals.py"] == body["project"]["files"]["signals.py"]


def test_move_preserves_runs_and_retry_does_not_resolve_changed_disk(project_setup):
    app, jobs, files, saved, body = project_setup
    first = jobs.create(PythonCreate.model_validate(body))
    moved = files.move(saved["path"], "改名后", saved["strategy_id"], saved["revision"])
    assert moved["strategy_id"] == saved["strategy_id"]
    assert jobs.create(PythonCreate.model_validate(body)).job_id == first.job_id
    second = jobs.create(PythonCreate.model_validate(body | {"idempotency_key": "after-move"}))
    assert second.request.strategy_id == first.request.strategy_id
    with TestClient(app) as client:
        response = client.get("/api/v1/python/backtests", params={"strategy_id": saved["strategy_id"]})
        assert response.status_code == 200 and response.json()["total"] == 2


@pytest.mark.parametrize("change", ["changed", "missing", "extra", "linked"])
def test_history_and_execution_reject_any_changed_source(project_setup, change, tmp_path):
    app, jobs, _, _, body = project_setup
    job = jobs.create(PythonCreate.model_validate(body))
    source = jobs.directory(job.job_id) / "source"
    helper = source / "unused/filters.py"
    if change == "changed":
        helper.write_text("FLAG = False\n")
    elif change == "missing":
        helper.unlink()
    elif change == "extra":
        (source / "extra.py").write_text("x = 1\n")
    else:
        helper.unlink()
        external = tmp_path / "outside.py"
        external.write_text(body["project"]["files"]["unused/filters.py"])
        helper.symlink_to(external)
    with pytest.raises(DomainError):
        jobs.check(jobs.repo.row(job.job_id), execution=True)
    with pytest.raises(DomainError):
        app.state.history.check(jobs.repo.row(job.job_id))


@pytest.mark.parametrize("change", ["both", "neither", "identity", "legacy_identity", "missing_entry", "path", "syntax"])
def test_invalid_project_is_rejected_before_acceptance(project_setup, change):
    app, jobs, _, _, body = project_setup
    if change == "both":
        body["source"] = "pass"
    elif change == "neither":
        del body["project"]
    elif change == "identity":
        body["strategy_id"] = "aqproj-" + "f" * 32
    elif change == "legacy_identity":
        body["strategy_id"] = "strategy.py"
    elif change == "missing_entry":
        body["project"]["entrypoint"] = "missing.py"
    elif change == "path":
        body["project"]["files"]["../outside.py"] = "pass"
    else:
        body["project"]["files"]["unused/filters.py"] = "def invalid("
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 422, response.text
        if change == "syntax":
            assert response.json()["error"]["details"]["source_file"] == "unused/filters.py"
        assert not jobs.repo.rows()


def test_project_and_legacy_source_over_256_kib_use_existing_resource_policy(project_setup):
    _, jobs, _, _, body = project_setup
    body["project"]["files"]["large.py"] = "#" + "x" * 300_000 + "\n"
    project = jobs.create(PythonCreate.model_validate(body))
    legacy = {k: v for k, v in body.items() if k not in {"project", "strategy_id"}}
    legacy.update(source=body["project"]["files"]["large.py"], idempotency_key="large-legacy")
    single = jobs.create(PythonCreate.model_validate(legacy))
    assert project.status == single.status == "queued"


def test_absent_project_keeps_old_frozen_manifest_bytes(setup):
    app, jobs, body = setup
    job = jobs.create(PythonCreate.model_validate(body))
    row = jobs.repo.row(job.job_id)
    original = row["manifest"].encode()
    assert b'"project"' not in original and b'"strategy_id"' not in original
    assert jobs.check(row).schema_version == "python-demand-job.v2"
    assert canonical(jobs.repo.manifest(row)) == original
    assert canonical(app.state.history.check(row)) == original
    assert row == jobs.repo.row(job.job_id)


def test_unicode_helper_survives_frozen_source_and_history(project_setup):
    app, jobs, _, _, body = project_setup
    body["project"]["files"]["café.py"] = "value = '咖啡'\n"
    job = jobs.create(PythonCreate.model_validate(body))
    row = jobs.repo.row(job.job_id)
    assert jobs.check(row).request.project.files["café.py"] == "value = '咖啡'\n"
    assert app.state.history.check(row)["request"]["project"] == body["project"]


def test_reopening_control_store_keeps_project_and_legacy_history(project_setup):
    _, jobs, _, _, body = project_setup
    project = jobs.create(PythonCreate.model_validate(body))
    legacy_body = {k: v for k, v in body.items() if k not in {"project", "strategy_id"}}
    legacy = jobs.create(PythonCreate.model_validate(legacy_body | {
        "source": "def initialize(context): pass\n", "idempotency_key": "legacy-reopen",
    }))
    before = {job_id: jobs.repo.row(job_id)["manifest"] for job_id in (project.job_id, legacy.job_id)}
    reopened = create_app(str(jobs.repo.store.path), diagnostics_configuration={})
    assert getattr(reopened.state, "database_error", None) is None
    with TestClient(reopened) as client:
        for job_id, original in before.items():
            response = client.get("/api/v1/history/jobs/" + job_id + "/manifest")
            assert response.status_code == 200, response.text
            assert canonical(response.json()).decode() == original
    assert {job_id: jobs.repo.row(job_id)["manifest"] for job_id in before} == before
