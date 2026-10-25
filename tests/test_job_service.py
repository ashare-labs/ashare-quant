"""Bounded control-plane and process tests with disposable stores only."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import lock, worker_slot
from ashare_quant.storage.upgrade import upgrade_copy
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.process import stop
from ashare_quant.tasks.repository import Repository
from tests.support.app import create_app
from tests.support.daily.executor import Configuration


def row(repo, number):
    job_id = "m2job-" + str(number).zfill(32)
    with repo.store.connection(transaction=True) as db:
        db.execute(
            "INSERT INTO tasks(job_id,idempotency_key,request_hash,request,manifest,manifest_sha256,account_id,status,created_at,updated_at) VALUES(?,?,?,'{}','{}',?,?,'queued',?,?)",
            (job_id, str(number), "0" * 64, "0" * 64, str(number), "2026-01-01", "2026-01-01"),
        )
    return job_id


def test_initial_policy_preserved_and_runtime_override_allowed(store):
    repo = Repository(store, ServicePolicy(max_workers=2, max_pending=3))
    assert Repository(store).policy == repo.policy
    changed = Repository(store, ServicePolicy(max_workers=1))
    assert changed.policy.max_workers == 1
    assert changed.initial_policy == repo.policy
    assert Repository(store).policy == repo.policy
    assert repo.status().available_pending == 3


def test_queue_capacity_fencing_and_attempt_budget(store):
    repo = Repository(store, ServicePolicy(max_pending=2, max_attempts=2))
    first, second = row(repo, 1), row(repo, 2)
    assert repo.candidates() == [first, second]
    with pytest.raises(DomainError) as error:
        repo.capacity()
    assert error.value.status == 429
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: repo.claim(first), range(4)))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    with store.connection(transaction=True) as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE job_id=?", (first,))
    replacement = repo.claim(first)
    with pytest.raises(DomainError) as error:
        repo.finish(first, claim["lease_token"], result={"untrusted": True})
    assert error.value.code == "M2_JOB_STALE_WORKER"
    repo.cancel(first)
    repo.finish(
        first,
        replacement["lease_token"],
        proof={"cursor": 0, "complete": False, "checkpoint_sha256": None},
    )
    assert repo.row(first)["status"] == "cancelled"
    repo.capacity()
    assert repo.resume(first)["attempt_count"] == 2
    assert repo.resume(first)["attempt_count"] == 2  # Queued duplicate grants nothing.
    assert repo.claim(first)["attempt_count"] == 3
    with store.connection(transaction=True) as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE job_id=?", (first,))
    assert repo.claim(first)["attempt_count"] == 4
    with store.connection(transaction=True) as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE job_id=?", (first,))
    assert repo.claim(first) is None
    assert json.loads(repo.row(first)["error"])["code"] == "M2_JOB_ATTEMPT_LIMIT"
    grants = [e for e in repo.logs(first) if e["event"] == "resume_queued"]
    assert len(grants) == 1
    assert grants[0]["details"]["attempt_base"] == 2


def test_execution_slots_and_admission_wait_are_bounded(tmp_path):
    started = time.monotonic()
    with worker_slot(tmp_path, 2), worker_slot(tmp_path, 2):
        with pytest.raises(DomainError) as error:
            with worker_slot(tmp_path, 2):
                pytest.fail("third execution slot acquired")
        assert error.value.code == "M2_JOB_WRITER_BUSY"
    with lock(tmp_path / "creation.lock"):
        with pytest.raises(DomainError) as error:
            with lock(tmp_path / "creation.lock", timeout=0.1):
                pytest.fail("lock should time out")
        assert error.value.code == "M2_JOB_ADMISSION_BUSY" and error.value.status == 503
    assert time.monotonic() - started < 2


def test_http_service_contract_and_untrusted_mutations(tmp_path):
    from ashare_quant.api import create_app as product_app

    app = product_app(str(tmp_path / "control.sqlite3"))
    with TestClient(app) as client:
        status = client.get("/api/v1/tasks/service")
        assert status.status_code == 200
        assert status.json()["contract_version"] == "m2-job-service.v1"
        assert status.json()["job_contract_version"] == "research-jobs.v1"
        for origin in (
            "https://evil.invalid",
            "null",
            "http://localhost.evil.invalid",
            "http://[broken",
            "http://localhost/path",
        ):
            for endpoint in (
                "/api/v1/python/jobs",
                "/api/v1/factors/jobs",
                "/api/v1/python/jobs",
                "/api/v1/python/jobs/m2job-" + "0" * 32 + "/cancel",
            ):
                response = client.post(endpoint, headers={"origin": origin}, json={})
                assert response.status_code == 403, response.text
                assert response.json()["error"]["code"] == "M2_JOB_ORIGIN_REFUSED"
        assert (
            client.post(
                "/api/v1/python/jobs", headers={"sec-fetch-site": "cross-site"}, json={}
            ).status_code
            == 403
        )
        # quant-web loopback proxy and local CLI clients remain compatible.
        for headers in (
            {},
            {"origin": "http://127.0.0.1:4173"},
            {"origin": "http://localhost:5173"},
        ):
            assert client.post("/api/v1/python/jobs", headers=headers, json={}).status_code == 422
        assert client.put("/api/v1/tasks/service", json={"max_workers": 100}).status_code == 405
        assert client.get("/api/v1/tasks/service?max_workers=100").status_code == 422
        assert client.get("/api/v1/python/jobs").json() == {"items": []}
        assert app.state.tasks.repo.status().policy.max_workers == 1
        scoped = client.get("/api/v1/tasks/openapi.json").json()
        assert "/api/v1/tasks/service" in scoped["paths"]


@pytest.mark.parametrize("kind", ["empty_old", "future", "malformed"])
def test_unknown_database_shapes_refuse_before_initialization(tmp_path, kind):
    path = tmp_path / "original.sqlite3"
    if kind == "malformed":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as db:
            if kind == "empty_old":
                db.execute("CREATE TABLE tasks (job_id TEXT, manifest TEXT)")
            else:
                db.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    with TestClient(create_app(str(path))) as client:
        response = client.get("/api/v1/tasks/daily/jobs")
        assert response.status_code == 409
        assert response.json()["error"]["details"]["database_modified"] is False
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("version", [3, 4, 5])
def test_side_by_side_upgrade_retains_immutable_old_jobs(tmp_path, version):
    source = tmp_path / "old.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE tasks(job_id TEXT, manifest TEXT, status TEXT)")
        db.execute(
            "INSERT INTO tasks VALUES('old',?,'queued')",
            (json.dumps({"schema_version": f"m2-job-manifest.v{version}"}),),
        )
    tasks = Path(str(source) + ".tasks")
    tasks.mkdir()
    (tasks / "native.chk").write_bytes(b"opaque original checkpoint")
    before = source.read_bytes()
    destination = tmp_path / "new"
    report = upgrade_copy(source, destination)
    assert not report["source_modified"] and not report["old_tasks_migrated"]
    assert source.read_bytes() == before == (destination / "archive" / source.name).read_bytes()
    assert (
        destination / "archive" / tasks.name / "native.chk"
    ).read_bytes() == b"opaque original checkpoint"
    with sqlite3.connect(destination / "historical.sqlite3") as db:
        assert db.execute("SELECT status FROM tasks").fetchone()[0] == "queued"
    with TestClient(create_app(str(destination / "control.sqlite3"))) as client:
        assert client.get("/api/v1/tasks/daily/jobs").json() == {"items": []}
    with pytest.raises(DomainError) as error:
        upgrade_copy(source, destination)
    assert error.value.code == "DATABASE_UPGRADE_DESTINATION_EXISTS"


def test_upgrade_wal_snapshot_preserves_original_sidecars(tmp_path):
    source = tmp_path / "wal.sqlite3"
    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE marker(value INTEGER)")
        db.execute("INSERT INTO marker VALUES(7)")
        db.commit()
        paths = [source, Path(str(source) + "-wal"), Path(str(source) + "-shm")]
        before = {p.name: p.read_bytes() for p in paths}
        upgrade_copy(source, tmp_path / "new")
        assert {p.name: p.read_bytes() for p in paths} == before
        with sqlite3.connect(tmp_path / "new/historical.sqlite3") as snap:
            assert snap.execute("SELECT value FROM marker").fetchone()[0] == 7
    finally:
        db.close()


def test_preparation_output_is_complete_without_a_retired_byte_gate(tmp_path, monkeypatch):
    from ashare_quant.tasks.executor import Configuration as RuntimeConfiguration
    from ashare_quant.tasks.executor import Executor as RuntimeExecutor
    executor = RuntimeExecutor(tmp_path, RuntimeConfiguration(tmp_path))
    command = "import pathlib; print('x' * 2000000); pathlib.Path('attempt.proof.json').write_text('{}')"
    monkeypatch.setattr(executor, "command", lambda *a: [sys.executable, "-c", command], raising=False)
    assert executor.immediate("prepare", tmp_path, "attempt") == {}
    assert (tmp_path / "attempt.runner.log").stat().st_size == 2000001


@pytest.mark.parametrize("mode", ["wall", "rss", "file", "cpu"])
def test_retired_process_gates_do_not_stop_valid_work(tmp_path, mode):
    policy = dict(cpu_seconds=1, execution_seconds=1, rss_bytes=64*1024*1024,
                  file_bytes=1024*1024, open_files=1)
    actions = {
        "wall": "time.sleep(1.5)",
        "cpu": "deadline=time.monotonic()+1.5; exec('while time.monotonic()<deadline: pass')",
        "rss": "data=bytearray(96*1024*1024); time.sleep(.2)",
        "file": "open('large','wb').write(b'x'*(2*1024*1024))",
    }
    code = (
        "import json,os,time; from ashare_quant.tasks.process import child_limits; "
        "child_limits(json.loads(os.environ['TEST_POLICY']),os.getppid(),'run','resource.json'); "
        + actions[mode]
    )
    process = subprocess.Popen([sys.executable,"-c",code],cwd=tmp_path,
                env=os.environ | {"TEST_POLICY":json.dumps(policy)},start_new_session=True)
    try:
        assert process.wait(timeout=8)==0
        assert not (tmp_path/'resource.json').exists()
        if mode=='file':
            assert (tmp_path/'large').stat().st_size==2*1024*1024
    finally:
        stop(process)


def test_stop_terminates_descendant_group(tmp_path):
    pidfile = tmp_path / "pid"
    child_code = "import fcntl,os,sys,time; from pathlib import Path; f=open(sys.argv[1]+'.lock','w'); fcntl.flock(f,fcntl.LOCK_EX); Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(20)"
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); time.sleep(20)"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(pidfile), child_code], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        assert int(pidfile.read_text()) > 0
        stop(process)
        assert process.returncode in {-signal.SIGTERM, -signal.SIGKILL}
        # The descendant holds its own lock until process exit; no process-list access needed.
        with lock(Path(str(pidfile) + ".lock"), timeout=2):
            pass
    finally:
        stop(process)


def test_v6_source_change_allows_load_but_retired_execution_is_refused(store):
    repo = Repository(store)
    job_id = row(repo, 9)
    with store.connection(transaction=True) as db:
        db.execute(
            "UPDATE tasks SET manifest=? WHERE job_id=?",
            (
                json.dumps(
                    {
                        "schema_version": "m2-job-manifest.v6",
                        "service_identity": {"source_sha256": "0" * 64},
                    }
                ),
                job_id,
            ),
        )
    before = store.path.read_bytes()
    from ashare_quant.api import create_app as product_app
    from ashare_quant.tasks.service import Jobs as ProductJobs

    with TestClient(product_app(str(store.path))) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.post(f"/api/v1/tasks/daily/jobs/{job_id}/resume", json={}).status_code == 404
    with pytest.raises(DomainError) as refused:
        ProductJobs(store).for_job(repo.row(job_id))
    assert refused.value.code == "TASK_FAMILY_RETIRED"
    assert store.path.read_bytes() == before


def test_exhausted_expired_writer_is_not_automatically_retried(store):
    repo = Repository(store, ServicePolicy(max_attempts=1))
    job_id = row(repo, 1)
    repo.claim(job_id)
    with store.connection(transaction=True) as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE job_id=?", (job_id,))
    assert repo.claim(job_id) is None
    final = repo.row(job_id)
    assert final["status"] == "failed" and final["attempt_count"] == 1
    assert json.loads(final["error"])["code"] == "M2_JOB_ATTEMPT_LIMIT"
    assert repo.candidates() == []


def test_upgrade_refuses_dangling_destination_link(tmp_path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE marker(value INTEGER)")
    target = tmp_path / "linked"
    target.symlink_to(tmp_path / "must-not-create", target_is_directory=True)
    before = source.read_bytes()
    with pytest.raises(DomainError) as error:
        upgrade_copy(source, target)
    assert error.value.code == "DATABASE_UPGRADE_DESTINATION_EXISTS"
    assert not (tmp_path / "must-not-create").exists() and source.read_bytes() == before


def test_interrupted_stop_still_kills_owned_process(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    script = "import signal,time,sys; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(20)"
    process = subprocess.Popen([sys.executable, "-c", script, str(ready)], start_new_session=True)
    original_wait = process.wait
    calls = [0]

    def interrupted(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise KeyboardInterrupt
        return original_wait(*args, **kwargs)

    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        monkeypatch.setattr(process, "wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            stop(process)
        assert original_wait(timeout=3) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            original_wait(timeout=5)


def test_unaccepted_preparation_directories_are_bounded_and_reported(tmp_path):
    app = create_app(
        str(tmp_path / "control.sqlite3"),
        daily_tasks_configuration=Configuration(None, ServicePolicy(max_pending=2)),
    )
    jobs = app.state.daily_tasks
    for n in range(2):
        jobs.directory("m2job-" + str(n).zfill(32)).mkdir()
    with TestClient(app) as client:
        status = client.get("/api/v1/tasks/daily/jobs/service").json()
        assert status["unaccepted_task_directories"] == 2
        response = client.post(
            "/api/v1/tasks/daily/jobs",
            json={
                "name": "retained preparation",
                "window_id": "two_day",
                "idempotency_key": "blocked",
                "assumption_ack_sha256": "0" * 64,
                "data_binding_sha256": "0" * 64,
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "M2_JOB_PREPARATION_RESIDUE_LIMIT"
        assert len(list(jobs.root.glob("m2job-*"))) == 2
