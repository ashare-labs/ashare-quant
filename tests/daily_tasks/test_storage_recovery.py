"""Storage failures return stable JSON and retry the same frozen submission safely."""

import errno
import json
import sqlite3

import pytest

import ashare_quant.tasks.daily.service as service
from ashare_quant.tasks.worker import work_one
from tests.daily_tasks.conftest import request


@pytest.mark.parametrize("fault,code", [
    ("disk_full", "STORAGE_FULL"),
    ("read_only_directory", "STORAGE_PERMISSION_DENIED"),
    ("read_only_filesystem", "STORAGE_PERMISSION_DENIED"),
    ("sqlite_full", "STORAGE_FULL"),
])
def test_storage_failure_and_original_key_retry(host, tmp_path, monkeypatch, fault, code):
    jobs, client = host
    payload = request(client, window="two_day", key="repair-storage")
    with monkeypatch.context() as patch:
        if fault == "read_only_directory":
            jobs.root.chmod(0o500)
        elif fault == "sqlite_full":
            def full(_):
                exc = sqlite3.OperationalError("controlled SQLITE_FULL before admission commit")
                exc.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise exc
            patch.setattr(jobs.repo, "insert", full)
        else:
            atomic = service.atomic

            def full(path, value):
                if path.name == "submission.json":
                    raise OSError(errno.ENOSPC if fault == "disk_full" else errno.EROFS, "controlled filesystem failure")
                return atomic(path, value)
            patch.setattr(service, "atomic", full)
        try:
            failed = client.post("/api/v1/tasks/daily/jobs", json=payload)
        finally:
            jobs.root.chmod(0o700)
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == code
    assert "original idempotency_key" in failed.json()["error"]["details"]["retry"]
    assert failed.headers["Retry-After"] == "2" and failed.headers["Cache-Control"] == "no-store"
    assert jobs.repo.rows() == []
    residue = jobs.unaccepted_directories()
    evidence_before = {str(p.relative_to(jobs.root)): p.read_bytes() for p in jobs.root.glob("m2job-*/**/*") if p.is_file()}
    accepted = client.post("/api/v1/tasks/daily/jobs", json=payload)
    assert accepted.status_code == 201, accepted.text
    retried = client.post("/api/v1/tasks/daily/jobs", json=payload)
    assert retried.status_code == 201 and retried.json()["job_id"] == accepted.json()["job_id"]
    assert len(jobs.repo.rows()) == 1
    assert all((jobs.root / name).read_bytes() == value for name, value in evidence_before.items())
    job_id = accepted.json()["job_id"]
    assert work_one(jobs) == job_id
    result = jobs.result(job_id)
    assert jobs.get(job_id).status == "succeeded"
    fills = [fill for day in result.days for fill in day.fills]
    assert len(fills) == len({fill.fill_id for fill in fills}) == result.summary.committed_fill_count
    assert work_one(jobs) is None
    (tmp_path / "storage-recovery-evidence.json").write_text(json.dumps({
        "fault": fault, "refusal": failed.json(), "retained_preparation_directories": residue,
        "retry_job_id": job_id, "accepted_accounts": len(jobs.repo.rows()), "unique_fills": len(fills),
        "status": jobs.get(job_id).status, "prior_residue_unchanged": True,
    }, indent=2) + "\n")
