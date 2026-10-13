"""Repeatable bounded load on the actual HTTP -> SQLite -> native child pipeline."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.m2_jobs.executor import Configuration
from ashare_quant.m2_jobs.policy import ServicePolicy
from ashare_quant.m2_jobs.worker import work_one

from .conftest import create, request


def test_bounded_parallel_queue_idempotency_and_result_trace(owner_store, tmp_path):
    owner, dataset = owner_store
    config = Configuration(owner, ServicePolicy(max_workers=2, max_pending=5))
    app = create_app(str(tmp_path / "control.sqlite3"), m2_jobs_configuration=config)
    jobs = app.state.m2_jobs
    with TestClient(app) as client:
        assert client.post("/api/v1/m2/data/bind", json={"dataset_id": dataset}).status_code == 200
        payloads = [request(client, window="m2a", key=f"load-{i}") for i in range(5)]

        def submit(index):
            for _ in range(12):
                response = client.post("/api/v1/m2/jobs", json=payloads[index])
                if response.status_code == 503:
                    assert response.json()["error"]["code"] == "M2_JOB_ADMISSION_BUSY"
                    assert response.headers["Retry-After"] == "2"
                    time.sleep(0.1)
                    continue
                assert response.status_code == 201, response.text
                return response.json()["job_id"]
            pytest.fail("bounded submission retries exhausted")

        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(submit, [0, 0, 1, 1, 2, 3, 4, 4]))
        assert len(set(ids)) == 5
        assert len(jobs.repo.rows()) == len(list(jobs.root.glob("m2job-*"))) == 5
        assert len({r["account_id"] for r in jobs.repo.rows()}) == 5
        overflow = client.post(
            "/api/v1/m2/jobs", json=payloads[0] | {"idempotency_key": "overflow"}
        )
        assert (
            overflow.status_code == 429 and overflow.json()["error"]["code"] == "M2_JOB_QUEUE_FULL"
        )
        assert submit(0) == ids[0]  # Existing key still works when full.
        conflict = client.post("/api/v1/m2/jobs", json=payloads[0] | {"name": "changed"})
        assert conflict.status_code == 409
        # Remove one queued task; cancellation makes capacity immediately available.
        cancelled = ids[-1]
        assert (
            client.post(f"/api/v1/m2/jobs/{cancelled}/cancel", json={}).json()["status"]
            == "cancelled"
        )
        assert jobs.repo.status().available_pending == 1
        active, maximum = set(), [0]
        observed = threading.Lock()
        barrier = threading.Barrier(2)

        def stage(name, service, row, process):
            if name == "spawned":
                with observed:
                    active.add(row["job_id"])
                    maximum[0] = max(maximum[0], len(active))
                barrier.wait(timeout=20)
            elif name == "child_exited":
                with observed:
                    active.discard(row["job_id"])

        def drain():
            while work_one(jobs, _stage=stage) is not None:
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(drain) for _ in range(2)]
            for future in futures:
                future.result(timeout=90)
        assert maximum[0] == 2
        assert jobs.get(cancelled).status == "cancelled"
        summaries = []
        for job_id in set(ids) - {cancelled}:
            view = jobs.get(job_id)
            assert view.status == "succeeded" and view.attempt_count == 1
            result = jobs.result(job_id)
            assert result.manifest_sha256 == view.manifest_sha256
            assert (
                result.strategy.version_id == jobs.check(jobs.repo.row(job_id)).strategy.version_id
            )
            history = result.recovery["control_events"]
            accepted = next(e for e in history if e["event"] == "queued")
            assert accepted["details"]["service_policy"]["max_workers"] == 2
            assert accepted["details"]["service_policy_sha256"] == jobs.repo.status().policy_sha256
            summaries.append(result.summary.model_dump(mode="json"))
            assert (
                client.put(f"/api/v1/m2/jobs/{job_id}/result", json={"cash": 0}).status_code == 405
            )
            assert (
                client.post(f"/api/v1/m2/jobs/{job_id}/resume", json={"parameters": {}}).status_code
                == 422
            )
        assert all(s == summaries[0] for s in summaries)
        evidence = {
            "schema": "job-service-load-evidence.v1",
            "submissions": 8,
            "unique_jobs": 5,
            "completed": 4,
            "queued_cancelled": 1,
            "max_native_children": maximum[0],
            "max_configured": 2,
            "queue_full_http": overflow.status_code,
            "idempotent_duplicate_preserved": True,
            "account_count": 5,
            "result_identity_and_policy_verified": True,
            "policy_sha256": jobs.repo.status().policy_sha256,
        }
        (tmp_path / "load-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


def test_frozen_inputs_and_stored_manifest_corruption_are_structured_refusals(host):
    jobs, client = host
    job_id = create(client, window="m2a")["job_id"]
    path = jobs.directory(job_id) / "parameters.json"
    original = path.read_bytes()
    path.write_text('{"initial_cash":"99999"}')
    response = client.get(f"/api/v1/m2/jobs/{job_id}/manifest")
    assert response.status_code == 409
    assert jobs.repo.row(job_id)["attempt_count"] == 0
    path.write_bytes(original)
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("UPDATE m2_jobs SET manifest='invalid json' WHERE job_id=?", (job_id,))
    response = client.get(f"/api/v1/m2/jobs/{job_id}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "M2_JOB_IDENTITY_CONFLICT"


def test_cancellation_after_publication_never_exposes_success(host):
    jobs, client = host
    job_id = create(client, window="m2a")["job_id"]

    def cancel(name, *_):
        if name == "after_publish":
            jobs.cancel(job_id)

    work_one(jobs, job_id, _stage=cancel)
    view = jobs.get(job_id)
    assert view.status == "cancelled" and view.checkpoint_complete and view.result is None
    assert client.get(f"/api/v1/m2/jobs/{job_id}/result").status_code == 409
    assert jobs.cancel(job_id) == view
    from ashare_quant.m2_jobs.files import sha

    checkpoint = sha((jobs.directory(job_id) / "native.chk").read_bytes())
    jobs.resume(job_id)
    work_one(jobs, job_id)
    assert jobs.get(job_id).status == "succeeded"
    assert jobs.get(job_id).account_id == view.account_id
    assert sha((jobs.directory(job_id) / "native.chk").read_bytes()) == checkpoint
    assert jobs.result(job_id).recovery["completion"]["native_run"] is False


def test_storage_commit_failure_keeps_checkpoint_recoverable(host):
    import sqlite3

    from ashare_quant.m2_jobs.files import sha

    jobs, client = host
    job_id = create(client, window="m2a")["job_id"]
    with jobs.repo.store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_publish BEFORE UPDATE OF status ON m2_jobs WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT,'injected storage commit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        work_one(jobs, job_id)
    before = jobs.get(job_id)
    assert before.status == "running" and before.result is None
    checkpoint = sha((jobs.directory(job_id) / "native.chk").read_bytes())
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("DROP TRIGGER fail_publish")
        db.execute("UPDATE m2_jobs SET lease_until=0 WHERE job_id=?", (job_id,))
    work_one(jobs, job_id)
    result = jobs.result(job_id)
    assert jobs.get(job_id).attempt_count == 2
    assert jobs.get(job_id).account_id == before.account_id
    assert result.recovery["completion"]["native_run"] is False
    assert sha((jobs.directory(job_id) / "native.chk").read_bytes()) == checkpoint
    assert client.get(f"/api/v1/m2/jobs/{job_id}/result").status_code == 200


def test_http_storage_write_refusal_is_structured_and_atomic(host):
    jobs, client = host
    job_id = create(client, window="m2a")["job_id"]
    with jobs.repo.store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_cancel BEFORE UPDATE OF status ON m2_jobs WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT,'injected write rejection'); END"
        )
    response = client.post(f"/api/v1/m2/jobs/{job_id}/cancel", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DATABASE_OPERATION_REFUSED"
    assert jobs.get(job_id).status == "queued"
    assert jobs.get(job_id).cancellation_requested_at is None
    with jobs.repo.store.connection() as db:
        db.execute("DROP TRIGGER fail_cancel")
    assert client.post(f"/api/v1/m2/jobs/{job_id}/cancel", json={}).json()["status"] == "cancelled"
