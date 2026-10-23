"""Control safety checks; these fixtures never claim real market execution."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.backtests.minute.api import Context
from ashare_quant.tasks.minute.contracts import PythonCreate
from tests.support.python_sources import SOURCE


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    if hasattr(request, "param"):
        import json

        monkeypatch.setenv("ASHARE_QUANT_JOB_POLICY", json.dumps({"max_pending": request.param}))
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    jobs = app.state.python_jobs
    release = SimpleNamespace(root=tmp_path, release_id=10, sha256="a" * 64)
    jobs.product = SimpleNamespace(call=lambda f: f(SimpleNamespace(release=release)))
    monkeypatch.setattr(
        "ashare_quant.tasks.minute.prepare.scope", lambda *a: (["2024-03-04", "2024-03-05"], [])
    )
    monkeypatch.setattr(jobs.executor, "package", lambda: tmp_path)
    monkeypatch.setattr(jobs, "environment", lambda: "a" * 64)
    body = dict(
        environment_id="a" * 64,
        name="User Python",
        idempotency_key="one",
        source=SOURCE,
        parameters={"shares": 100},
        securities=["000001.XSHE"],
        start_date="2024-03-04",
        end_date="2024-03-05",
        initial_cash=100000,
        accept_research_model=True,
        trusted_local_code=True,
    )
    return app, jobs, body


def test_source_and_parameters_are_frozen_and_queue_is_shared(setup):
    app, jobs, body = setup
    with TestClient(app) as client:
        first = client.post("/api/v1/python/jobs", json=body)
        assert first.status_code == 201, first.text
        job = first.json()
        same = client.post("/api/v1/python/jobs", json=body).json()
        assert same["job_id"] == job["job_id"]
        assert client.get("/api/v1/tasks/daily/jobs").status_code == 404
        assert client.get("/api/v1/portfolio/jobs").status_code == 404
        assert len(client.get("/api/v1/python/jobs").json()["items"]) == 1
        assert not job["can_resume"]
        root = jobs.directory(job["job_id"])
        assert (root / "strategy.py").read_text() == SOURCE
        assert (
            app.state.tasks.for_job(jobs.repo.row(job["job_id"])).__class__ is jobs.__class__
        )
        conflict = client.post("/api/v1/python/jobs", json=body | {"parameters": {"shares": 200}})
        assert conflict.status_code == 409
        assert not (root / "input.json").exists()  # HTTP admission does not prepare or execute.
        stopped = client.post("/api/v1/python/jobs/" + job["job_id"] + "/cancel", json={}).json()
        assert stopped["status"] == "cancelled" and not stopped["can_resume"]
        assert client.get("/api/v1/python/jobs/" + job["job_id"] + "/result").status_code == 409


def test_simultaneous_duplicate_requests_create_one_task(setup):
    _, jobs, body = setup
    request = PythonCreate.model_validate(body)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: jobs.create(request), range(2)))
    assert results[0].job_id == results[1].job_id
    assert len(jobs.repo.rows()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"source": "def initialize(:"},
        {"source": "print('no callback')"},
        {"accept_research_model": False},
        {"trusted_local_code": False},
        {"securities": ["000001.XSHE", "000001.XSHE"]},
        {"environment_id": "b" * 64},
        {"start_date": "2023-01-01"},
        {"initial_cash": 0},
    ],
)
def test_invalid_source_scope_or_consent_creates_no_task(setup, change):
    app, jobs, body = setup
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body | change)
        assert response.status_code in {409, 422}, response.text
        assert not jobs.repo.rows()


def test_foreign_origin_cannot_submit_python(setup):
    app, jobs, body = setup
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/python/jobs", json=body, headers={"origin": "https://untrusted.example"}
        )
        assert response.status_code == 403
        assert not jobs.repo.rows()


def test_changed_frozen_source_cannot_execute(setup):
    _, jobs, body = setup
    job = jobs.create(PythonCreate.model_validate(body))
    (jobs.directory(job.job_id) / "strategy.py").write_text("raise Exception('modified')")
    with pytest.raises(Exception, match="Frozen source"):
        jobs.check(jobs.repo.row(job.job_id), execution=True)


def test_context_parameters_are_detached_from_the_frozen_request():
    original = {"nested": {"size": 100}}
    context = Context(
        SimpleNamespace(parameters=original, data=SimpleNamespace(ids=["000001.XSHE"]))
    )
    context.parameters["nested"]["size"] = 200
    assert original["nested"]["size"] == 100
    assert context.securities == ("000001.XSHE",)


def test_worker_reentry_on_terminal_python_job_does_not_replay(setup):
    from ashare_quant.tasks.worker import work_one
    app, jobs, body = setup
    job = jobs.create(PythonCreate.model_validate(body))
    row = jobs.repo.claim(job.job_id)
    jobs.repo.finish(job.job_id, row["lease_token"], error={"code": "PYTHON_TEST_STOPPED"})
    assert work_one(app.state.tasks, job.job_id) is None
    assert jobs.repo.row(job.job_id)["attempt_count"] == 1


def test_worker_does_not_claim_pending_python_from_another_source(setup):
    import json

    from ashare_quant.contracts import DomainError
    from ashare_quant.storage.artifacts import atomic, canonical, digest
    from ashare_quant.tasks.worker import work_one

    app, jobs, body = setup
    job = jobs.create(PythonCreate.model_validate(body))
    row = jobs.repo.row(job.job_id)
    manifest = json.loads(row["manifest"])
    manifest["service_identity"]["source_sha256"] = "b" * 64
    root = jobs.directory(job.job_id)
    atomic(root / "accepted-manifest.json", manifest)
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("UPDATE tasks SET manifest=?,manifest_sha256=? WHERE job_id=?",
                   (canonical(manifest).decode(), digest(manifest), job.job_id))
    view = jobs.get(job.job_id)
    assert view.status == view.stored_status == "queued"
    assert view.display_status == view.phase == "runtime_unavailable"
    assert view.execution_reason["code"] == "M2_JOB_SERVICE_CHANGED"
    before = jobs.repo.row(job.job_id)
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert work_one(app.state.tasks) is None
    with pytest.raises(DomainError) as error:
        work_one(app.state.tasks, job.job_id)
    assert error.value.code == "M2_JOB_SERVICE_CHANGED"
    assert jobs.repo.row(job.job_id) == before
    assert files == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("setup", [1], indirect=True)
def test_historical_queue_does_not_block_new_python_or_factor_admission(setup):
    import json

    from ashare_quant.contracts import DomainError
    from ashare_quant.storage.artifacts import atomic, canonical, digest
    from ashare_quant.tasks.factors.service import FactorJobs

    app, jobs, body = setup
    historical = []
    for index, schema in enumerate(("m2-job-manifest.v6", "python-minute-job.v1")):
        job = jobs.create(PythonCreate.model_validate(body | {"idempotency_key": f"old-{index}"}))
        manifest = json.loads(jobs.repo.row(job.job_id)["manifest"])
        manifest["schema_version"] = schema
        manifest["service_identity"]["source_sha256"] = "b" * 64
        atomic(jobs.directory(job.job_id) / "accepted-manifest.json", manifest)
        with jobs.repo.store.connection(transaction=True) as db:
            db.execute("UPDATE tasks SET manifest=?,manifest_sha256=? WHERE job_id=?",
                       (canonical(manifest).decode(), digest(manifest), job.job_id))
        historical.append(jobs.repo.row(job.job_id))
    status = app.state.tasks.service_status()
    assert status.counts == {} and status.historical_counts == {"queued": 2}
    assert status.available_pending == 1
    new = jobs.create(PythonCreate.model_validate(body | {"idempotency_key": "current"}))
    assert app.state.tasks.repo.candidates() == [new.job_id]
    assert app.state.tasks.service_status().available_pending == 0
    # Both families enforce the same one-slot queue, including the other's jobs.
    factors = FactorJobs(app.state.store)
    for repository in (jobs.repo, factors.repo):
        with pytest.raises(DomainError) as error:
            repository.capacity()
        assert error.value.code == "M2_JOB_QUEUE_FULL"
    jobs.cancel(new.job_id)
    factors.repo.capacity()
    assert app.state.tasks.service_status().available_pending == 1
    assert [jobs.repo.row(row["job_id"]) for row in historical] == historical
