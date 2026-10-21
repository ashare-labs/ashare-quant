"""Control safety checks; these fixtures never claim real market execution."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.backtests.minute.api import Context
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.example import SOURCE


@pytest.fixture
def setup(tmp_path, monkeypatch):
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
        assert client.get("/api/v1/tasks/daily/jobs").json()["items"] == []
        assert client.get("/api/v1/portfolio/jobs").json()["items"] == []
        assert len(client.get("/api/v1/python/jobs").json()["items"]) == 1
        assert not job["can_resume"]
        root = jobs.directory(job["job_id"])
        assert (root / "strategy.py").read_text() == SOURCE
        assert (
            app.state.daily_tasks.for_job(jobs.repo.row(job["job_id"])).__class__ is jobs.__class__
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
    assert work_one(app.state.daily_tasks, job.job_id) is None
    assert jobs.repo.row(job.job_id)["attempt_count"] == 1
