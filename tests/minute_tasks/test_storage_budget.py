"""Real admission/worker regression for retained history versus per-task storage."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, digest
from ashare_quant.tasks import storage_budget
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.repository import Repository
from ashare_quant.tasks.worker import work_one
from tests.factors.test_exploration import app as app
from tests.factors.test_exploration import request as factor_request
from tests.factors.test_exploration import source as source
from tests.minute_tasks.test_api import setup as setup
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import fixed_release as fixed_release


def sparse(path, size):
    with path.open("wb") as stream:
        stream.truncate(size)


def test_history_over_budget_never_consumes_new_task_allowance(setup):
    app, jobs, body = setup
    old = jobs.create(PythonCreate.model_validate(body))
    jobs.cancel(old.job_id)
    history = jobs.directory(old.job_id)
    sparse(history / "retained-input", jobs.repo.policy.artifact_bytes + 1)
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in history.iterdir()}
    with TestClient(app) as client:
        accepted = client.post("/api/v1/python/jobs", json=body | {"idempotency_key": "new"})
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["job_id"] != old.job_id
        assert client.get("/api/v1/tasks/service").json()["artifact_budget_scope"] == "per_task"
    assert before == {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in history.iterdir()}


def test_pending_reservations_and_idempotency_use_real_free_space(setup, monkeypatch):
    app, jobs, body = setup
    first = jobs.create(PythonCreate.model_validate(body))
    policy = jobs.repo.policy
    pending_size = storage_budget.artifact_size(jobs.directory(first.job_id))
    # Enough for a new task alone, but not the existing task's remaining budget.
    free = policy.disk_free_bytes + policy.artifact_bytes
    monkeypatch.setattr(storage_budget.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    with TestClient(app) as client:
        refused = client.post("/api/v1/python/jobs", json=body | {"idempotency_key": "new"})
        assert refused.status_code == 507, refused.text
        error = refused.json()["error"]
        assert error["code"] == "M2_JOB_STORAGE_RESERVE"
        assert error["details"]["reserved_bytes"] == policy.artifact_bytes - pending_size
        assert len(jobs.repo.rows()) == 1
        # Retries neither reserve twice nor rerun a previously accepted strategy.
        repeated = client.post("/api/v1/python/jobs", json=body)
        assert repeated.status_code == 201 and repeated.json()["job_id"] == first.job_id
        jobs.cancel(first.job_id)
        assert client.post("/api/v1/python/jobs", json=body | {"idempotency_key": "new"}).status_code == 201


def test_low_disk_refuses_before_any_task_files(setup, monkeypatch):
    app, jobs, body = setup
    monkeypatch.setattr(storage_budget.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 507
        assert response.json()["error"]["details"]["observed"] == 1
    assert not jobs.repo.rows() and not list(jobs.root.glob("m2job-*"))


def test_existing_policy_document_is_not_rewritten(setup):
    _, jobs, _ = setup
    old = jobs.repo.policy.model_dump(mode="json")
    old.pop("disk_free_bytes")
    old.pop("retained_artifact_bytes")
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("UPDATE task_service SET policy=?,policy_sha256=?", (canonical(old).decode(), digest(old)))
    restored = Repository(jobs.repo.store)
    assert restored.policy.disk_free_bytes == 1073741824
    assert restored.policy.retained_artifact_bytes == 512000000000
    with jobs.repo.store.connection() as db:
        row = db.execute("SELECT policy,policy_sha256 FROM task_service").fetchone()
    assert tuple(row) == (canonical(old).decode(), digest(old))


def test_total_retention_budget_is_decimal_gb_and_never_deletes_history(setup):
    app, jobs, body = setup
    old = jobs.create(PythonCreate.model_validate(body))
    jobs.cancel(old.job_id)
    history = jobs.directory(old.job_id) / "retained-history"
    sparse(history, 512000000000)  # Sparse metadata fixture, not a 512 GB allocation.
    signature = (history.stat().st_size, history.stat().st_mtime_ns)
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body | {"idempotency_key": "new"})
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "M2_JOB_RETENTION_LIMIT"
        assert error["details"]["limit"] == 512000000000
        assert error["details"]["scope"] == "all_retained_tasks"
        assert len(jobs.repo.rows()) == 1
    assert signature == (history.stat().st_size, history.stat().st_mtime_ns)


def test_aggregate_counts_nested_files_and_excludes_external_links(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    cache = root / "execution-cache"
    cache.mkdir()
    sparse(cache / "input", 600000)
    sparse(root / "events", 500000)
    outside = tmp_path / "outside"
    outside.mkdir()
    sparse(outside / "input", 2000000)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "linked-file").symlink_to(outside / "input")
    with pytest.raises(DomainError) as error:
        storage_budget.check_task_storage(root, ServicePolicy(artifact_bytes=1048576))
    assert error.value.code == "M2_JOB_ARTIFACT_LIMIT"
    assert error.value.details["observed"] == 1100000
    assert error.value.details["scope"] == "per_task"


@pytest.mark.parametrize("daily_jobs", [{"artifact_bytes": 1048576}], indirect=True)
@pytest.mark.parametrize("wait", [0, 2])
def test_running_and_fast_finished_native_overflow_never_publish(daily_jobs, wait):
    _, jobs, body = daily_jobs
    body.update(source=f"""import time
def initialize(context):
    with open('oversized', 'wb') as stream:
        stream.write(b'x' * (2 * 1024 * 1024))
    time.sleep({wait})
""", end_date=body["start_date"])
    job = jobs.create(PythonCreate.model_validate(body))
    work_one(jobs, job.job_id)
    result = jobs.get(job.job_id)
    assert result.status == "failed" and result.result is None
    assert result.error["code"] == "M2_JOB_ARTIFACT_LIMIT", result.error
    assert result.error["details"]["observed"] >= 2097152
    root = jobs.directory(job.job_id)
    assert (root / "oversized").stat().st_size == 2097152  # Evidence is not deleted.
    assert not (root / "published.json").exists()
    assert work_one(jobs, job.job_id) is None


@pytest.mark.parametrize("daily_jobs", [{"artifact_bytes": 1048576}], indirect=True)
def test_publication_itself_is_included_in_task_budget(daily_jobs):
    _, jobs, body = daily_jobs
    body.update(source="def initialize(context): pass", end_date=body["start_date"])
    job = jobs.create(PythonCreate.model_validate(body))
    root = jobs.directory(job.job_id)

    def stage(name, *_):
        if name == "before_publish":
            sparse(root / "padding", jobs.repo.policy.artifact_bytes - storage_budget.artifact_size(root) - 1)

    work_one(jobs, job.job_id, _stage=stage)
    result = jobs.get(job.job_id)
    assert result.status == "failed" and result.result is None
    assert result.error["code"] == "M2_JOB_ARTIFACT_LIMIT", result.error
    assert result.error["details"]["additional_bytes"] > 1
    assert not (root / "published.json").exists()


def test_factor_admission_has_same_history_and_disk_contract(app, monkeypatch):
    jobs = app.state.factor_jobs
    root = jobs.root / ("m2job-" + "9" * 32)
    root.mkdir()
    sparse(root / "history", jobs.repo.policy.artifact_bytes + 1)
    body = factor_request(app)
    with TestClient(app) as client:
        response = client.post("/api/v1/factors/jobs", json=body)
        assert response.status_code == 201, response.text
        monkeypatch.setattr(storage_budget.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
        repeated = client.post("/api/v1/factors/jobs", json=body)
        assert repeated.json()["job_id"] == response.json()["job_id"]
        denied = client.post("/api/v1/factors/jobs", json=body | {"idempotency_key": "new"})
        assert denied.status_code == 507 and denied.json()["error"]["code"] == "M2_JOB_STORAGE_RESERVE"
