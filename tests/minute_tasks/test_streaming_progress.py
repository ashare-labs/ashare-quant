"""Actual task progress, bounded cache lifetime and lossless streamed evidence."""

import gc
import json
import subprocess
import sys
import weakref

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical
from ashare_quant.storage.streams import Journal, atomic_json, file_hash, reading
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from ashare_quant.tasks.minute.evidence import verify_demand
from ashare_quant.tasks.progress import EngineProgress, RuntimeProgress, snapshot
from tests.minute_tasks.evidence import evidence
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import execute
from tests.minute_tasks.test_daily import fixed_release as fixed_release


def test_atomic_stream_is_byte_identical_and_journal_roundtrips(tmp_path):
    values = [{"security": "000001.XSHE", "day": i, "value": "中文"} for i in range(3000)]
    target = tmp_path / "result.json"
    assert (
        atomic_json(target, values) == __import__("hashlib").sha256(canonical(values)).hexdigest()
    )
    assert target.read_bytes() == canonical(values)
    journal = Journal(tmp_path / "queries.jsonl.gz")
    for value in values:
        journal.append(value)
    seal = journal.seal()
    assert seal["records"] == len(values) and file_hash(journal.path) == seal["sha256"]
    assert list(journal) == values
    assert not hasattr(journal, "values")


def test_sampling_is_small_single_snapshot_and_terminal_state_is_authoritative(tmp_path):
    row = {
        "job_id": "m2job-00000000000000000000000000000001",
        "attempt_count": 1,
        "status": "running",
    }
    engine = EngineProgress(tmp_path, "attempt-1")
    progress = RuntimeProgress(tmp_path, "m2job-00000000000000000000000000000001", 1)
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        progress.attach(process)
        engine.update(
            phase="executing",
            completed_trading_days=2,
            total_trading_days=3,
            simulation_date="2024-01-03",
            force=True,
        )
        progress.sample()
        first = snapshot(tmp_path, row)
        assert (
            first["current_rss_bytes"] > 0 and first["peak_rss_bytes"] >= first["current_rss_bytes"]
        )
        assert first["completed_trading_days"] == 2
        progress.sample()
        second = snapshot(tmp_path, row)
        assert (
            second["sequence"] > first["sequence"]
            and second["elapsed_seconds"] >= first["elapsed_seconds"]
        )
        assert (tmp_path / "progress.json").stat().st_size < 2048
        assert sorted(p.name for p in tmp_path.iterdir()) == ["phase.json", "progress.json"]
        terminal = snapshot(tmp_path, row | {"status": "cancelled"})
        assert terminal["terminal"] and terminal["current_rss_bytes"] is None
        assert snapshot(tmp_path, row | {"attempt_count": 2}) is None
    finally:
        process.terminate()
        process.wait()
        progress.finish()
    frozen = snapshot(tmp_path, row | {"status": "failed"})
    assert frozen["terminal"] and frozen["last_rss_bytes"] > 0
    (tmp_path / "progress.json").write_text("{broken")
    assert snapshot(tmp_path, row) is None


@pytest.mark.parametrize(
    "fixed_release", [{"minutes": True, "minute_range": [65, 68]}], indirect=True
)
def test_day_component_is_released_and_cross_day_history_remains_full(
    daily_jobs, fixed_release, tmp_path
):
    _, _, body = daily_jobs
    release, days = fixed_release
    body.update(frequency="1m", start_date=days[65], end_date=days[68], securities=[])
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path)
    try:
        old = data.component("600000.XSHG", data.days[0])
        ref = weakref.ref(old)
        del old
        data.component("600000.XSHG", data.days[1])
        gc.collect()
        assert ref() is None and len(data.components) == 1 and len(data.bars) == 240
        from ashare_quant.backtests.minute.inputs import stamp

        for day in data.days[1:]:
            rows = data.daily_history(
                "000001.XSHE", stamp(day + "T09:30:00+08:00"), count=60, fields=["close"]
            )
            assert len(rows) == 60 and rows[-1]["date"] < day
            data.component("600000.XSHG", day)
            assert (
                len(data.history_rows) == 60 and len(data.components) == 1 and len(data.bars) == 240
            )
        data.finalize()
        verify_demand(tmp_path)
        frozen = evidence(tmp_path)
        assert len([q for q in frozen["queries"] if q["api"] == "daily_history"]) == 3
        path = next((tmp_path / "execution-cache").glob("*/input.json.gz"))
        with reading(path) as stream:
            assert json.loads(stream.read())
        with path.open("ab") as stream:
            stream.write(b"changed")
        with pytest.raises(DomainError):
            verify_demand(tmp_path)
    finally:
        data.close()


def test_http_progress_refresh_result_and_idempotency_share_same_job(daily_jobs):
    app, jobs, body = daily_jobs
    body.update(securities=[], end_date=body["start_date"], source="def initialize(context): pass")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        p = job["progress"]
        assert p["terminal"] and p["completed_trading_days"] == p["total_trading_days"] == 1
        assert p["phase"] == "publishing" and p["current_rss_bytes"] is None
        assert client.get("/api/v1/python/jobs/" + job["job_id"]).json()["progress"] == p
        assert client.get("/api/v1/history/jobs/" + job["job_id"]).json()["progress"] == p
        assert client.post("/api/v1/python/jobs", json=body).json()["job_id"] == job["job_id"]


@pytest.mark.parametrize("expression,code", [
    ("MemoryError('simulated allocation failure')", "PYTHON_MEMORY_ALLOCATION_FAILED"),
    ("OSError(28, 'simulated disk write failure')", "PYTHON_STORAGE_WRITE_FAILED"),
])
def test_actual_resource_error_type_is_reported_without_claiming_budget_exhaustion(
    daily_jobs, expression, code
):
    app, jobs, body = daily_jobs
    body.update(securities=[], end_date=body["start_date"],
                source=f"def initialize(context): raise {expression}\n")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert job["error"]["code"] == code, job["error"]
        assert job["progress"]["terminal"] and job["progress"]["current_rss_bytes"] is None
        assert client.get(f"/api/v1/python/jobs/{job['job_id']}/result").status_code == 409
