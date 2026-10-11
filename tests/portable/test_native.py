"""Portable real-data execution, negative controls and durable native recovery."""

import shutil
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.executor import Configuration
from ashare_quant.m2_jobs.files import sha
from ashare_quant.m2_jobs.service import Jobs
from ashare_quant.m2_jobs.worker import work_one
from ashare_quant.store import Store

from .conftest import create, request


def complete(jobs, job_id):
    assert work_one(jobs, job_id) == job_id
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id)
    return jobs.result(job_id)


@pytest.mark.parametrize("window,count", [("m2a", 2), ("w1", 10), ("w2", 10)])
def test_three_real_windows(host, window, count):
    jobs, client = host
    job = create(client, window)
    result = complete(jobs, job["job_id"])
    assert len(result.days) == result.summary.actual_history_call_count == count
    assert result.execution_identity.engine == "RQAlpha"
    assert result.execution_identity.source_sha256 == result.service_identity.source_sha256
    assert (
        result.data_identity.external_owner_review_status
        == "local_contract_verified_not_independently_reviewed"
    )
    assert result.data_identity.owner_contract_review.scope == "data_production_contract_only"
    assert not result.data_identity.owner_contract_review.execution_permission
    assert result.data_identity.original_owner_envelope_review == "PENDING"
    assert result.service_identity.independent_review_status == "not_reviewed"
    assert not result.historical_pit_verified and not result.paper_admitted
    assert result.valuation_bar_query_observed == any(
        r.operation == "valuation_bar" for r in result.engine_price_reads
    )
    assert result.data_identity.original_quality["source_authenticity_verified"] is False
    assert result.data_identity.original_quality["daily_query_complete"] is False
    assert len(result.assumptions) == 7
    for day in result.days:
        assert day.previous_open < day.trading_date
        assert day.ad08.history_call["history_dt"] == day.previous_open.isoformat()
        assert day.ad08.first_call is not None and day.ad08.cache_hit
    with TestClient(
        create_app(str(jobs.repo.store.path), m2_jobs_configuration=Configuration(None))
    ) as offline:
        assert offline.get(
            "/api/v1/m2/jobs/" + job["job_id"] + "/result"
        ).json() == result.model_dump(mode="json")
        assert offline.get("/api/v1/m2/jobs/" + job["job_id"] + "/manifest").status_code == 200
    assert work_one(jobs, job["job_id"]) is None


@pytest.mark.parametrize(
    "parameters,orders",
    [
        ({"initial_cash": "1000", "target_shares": 200}, 0),
        ({"target_shares": 200}, None),
        ({"enter_below": "0.01", "exit_above": "1000000"}, 0),
    ],
)
def test_parameter_paths_use_native_book(host, parameters, orders):
    jobs, client = host
    config = request(client)["parameters"] | parameters
    result = complete(jobs, create(client, parameters=config)["job_id"])
    if orders is not None:
        assert result.summary.committed_order_count == orders
    else:
        assert all(f.quantity == 200 for day in result.days for f in day.fills)
    if parameters.get("initial_cash") == "1000":
        assert Decimal(result.summary.final_cash) == Decimal("1000.00")
        assert any(day.native_rejections for day in result.days)


@pytest.mark.parametrize(
    "fault", ["after_replace:1", "native_submit:2", "before_replace:30", "after_replace:29"]
)
def test_crash_and_resume_keeps_account(host, fault):
    jobs, client = host
    baseline = complete(jobs, create(client, key="baseline")["job_id"])
    job = create(client, key="crash")
    job_id = job["job_id"]
    work_one(jobs, job_id, _fault=fault)
    stopped = jobs.get(job_id)
    assert stopped.status == "failed" and stopped.result is None and stopped.can_resume
    jobs.resume(job_id)
    result = complete(jobs, job_id)
    assert result.summary == baseline.summary and result.account == baseline.account
    assert [(d.signal, d.account_at_day_close) for d in result.days] == [
        (d.signal, d.account_at_day_close) for d in baseline.days
    ]
    assert jobs.get(job_id).account_id == job["account_id"]
    assert jobs.get(job_id).session_sha256 == job["session_sha256"]
    assert (
        len({f.fill_id for d in result.days for f in d.fills})
        == result.summary.committed_fill_count
    )


def test_cancel_then_relocate_both_state_and_data(host, tmp_path):
    jobs, client = host
    job = create(client)
    job_id = job["job_id"]
    baseline = complete(jobs, create(client, key="baseline")["job_id"])

    def cancel(name, service, *args):
        if name == "child_exited":
            service.cancel(job_id)

    work_one(jobs, job_id, _fault="before_replace:30", _stage=cancel)
    assert jobs.get(job_id).status == "cancelled" and jobs.get(job_id).committed_cursor == 29
    original_db, original_root = jobs.repo.store.path, jobs.root
    target = tmp_path / "relocated"
    target.mkdir()
    # No open native writer remains. Move control DB and frozen task directory together.
    shutil.move(str(original_db), target / "control.sqlite3")
    shutil.move(str(original_root), target / "control.sqlite3.m2-jobs")
    copied_owner = target / "owner"
    shutil.copytree(jobs.config.owner_store, copied_owner)
    moved = Jobs(Store(target / "control.sqlite3"), Configuration(copied_owner))
    moved.resume(job_id)
    result = complete(moved, job_id)
    assert result.summary == baseline.summary and result.account == baseline.account
    assert moved.get(job_id).account_id == job["account_id"]
    assert moved.get(job_id).manifest_sha256 == job["manifest_sha256"]
    assert not original_db.exists() and not original_root.exists()
    assert (
        str(original_root) not in (moved.directory(job_id) / "accepted-manifest.json").read_text()
    )


@pytest.mark.parametrize("stage_name", ["before_publish", "after_publish"])
def test_completed_checkpoint_reentry_does_not_rerun_native(host, stage_name):
    jobs, client = host
    job_id = create(client)["job_id"]

    def crash(name, *args):
        if name == stage_name:
            raise RuntimeError("controlled publication failure")

    work_one(jobs, job_id, _stage=crash)
    assert jobs.get(job_id).checkpoint_complete and jobs.get(job_id).result is None
    path = jobs.directory(job_id)
    before = sha((path / "native.chk").read_bytes())
    jobs.resume(job_id)
    result = complete(jobs, job_id)
    assert result.recovery["completion"]["native_run"] is False
    assert sha((path / "native.chk").read_bytes()) == before
    assert not (path / "attempt-2.trace.jsonl").exists()


def test_stale_binding_ack_scope_and_idempotency(host):
    jobs, client = host
    original = request(client)
    for field in ["data_binding_sha256", "assumption_ack_sha256"]:
        assert client.post("/api/v1/m2/jobs", json=original | {field: "0" * 64}).status_code == 409
    assert (
        client.post("/api/v1/m2/jobs", json=original | {"window_id": "arbitrary"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/m2/data/bind", json={"dataset_id": "0" * 64, "store": "/tmp"}
        ).status_code
        == 422
    )
    previous = client.get("/api/v1/m2/jobs/catalogue").json()
    assert client.post("/api/v1/m2/data/bind", json={"dataset_id": "0" * 64}).status_code == 422
    assert client.get("/api/v1/m2/jobs/catalogue").json() == previous
    job = create(client)
    jobs.config = Configuration(None)
    assert (
        jobs.create(
            type(jobs.repo.manifest(jobs.repo.row(job["job_id"])).request).model_validate(original)
        ).job_id
        == job["job_id"]
    )
    assert client.post("/api/v1/m2/jobs", json=original | {"name": "changed"}).status_code == 409
    root = jobs.directory(job["job_id"])
    raw = (root / "data-binding.json").read_bytes()
    (root / "data-binding.json").write_bytes(raw.replace(b"UNVERIFIED", b"VERIFIED"))
    with pytest.raises(DomainError):
        jobs.get(job["job_id"])


def test_published_result_detects_evidence_change(host):
    jobs, client = host
    job_id = create(client)["job_id"]
    complete(jobs, job_id)
    file = jobs.directory(job_id) / jobs.get(job_id).result.publication / "data-binding.json"
    file.write_bytes(file.read_bytes() + b" ")
    with pytest.raises(DomainError) as exc:
        jobs.result(job_id)
    assert exc.value.code == "M2_JOB_RESULT_CONFLICT"
