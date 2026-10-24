"""Legal publications remain readable without a second byte threshold."""

import json
import tempfile
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, sha
from ashare_quant.tasks.evidence import evidence_hash, project_json, verified_copy
from ashare_quant.tasks.executor import Configuration
from ashare_quant.tasks.policy import ServicePolicy
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import execute
from tests.minute_tasks.test_daily import fixed_release as fixed_release


@pytest.mark.parametrize("value", [None, True, -1.25e30, "引号\"与\\路径", ["[]{}", {}, [None, 1]],
                                  {"nested": {"text": "x\\\"{}[]", "array": [True, False]}}])
def test_projection_matches_json_values_and_skips_unrequested_fields(value):
    document = {"skipped": value, "selected": value, "last": [1, 2]}
    for raw in (canonical(document), json.dumps(document, indent=2).encode()):
        with tempfile.TemporaryFile() as stream:
            stream.write(raw)
            stream.flush()
            assert project_json(stream, {"selected", "last"}) == {"selected": value, "last": [1, 2]}


@pytest.mark.parametrize("raw", [b"{} trailing", b'{"a":1,}', b'{"a":1,"a":2}', b'{"a":[}', b'{"a":"'])
def test_malformed_projection_refuses(raw):
    with tempfile.TemporaryFile() as stream:
        stream.write(raw)
        stream.flush()
        with pytest.raises((ValueError, IndexError)):
            project_json(stream, {"a"})


def test_verified_snapshot_prevents_response_race_and_checks_paths(tmp_path):
    raw = canonical({"job_id": "old", "logs": ["not retained in memory"]})
    path = tmp_path / "published.json"
    path.write_bytes(raw)
    with verified_copy(tmp_path, path.name, sha(raw), ServicePolicy()) as frozen:
        path.write_bytes(b'{"job_id":"changed"}')
        assert frozen.read() == raw
    with pytest.raises(DomainError, match="Published result differs"):
        with verified_copy(tmp_path, path.name, sha(raw), ServicePolicy()):
            pass
    (tmp_path / "linked").symlink_to(path)
    for name in ("../published.json", str(path), "linked"):
        with pytest.raises(DomainError) as refused:
            evidence_hash(tmp_path, name)
        assert refused.value.code == "HISTORY_EVIDENCE_PATH"


def test_verified_read_reports_actual_disk_reserve(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import ashare_quant.tasks.storage_budget as storage

    (tmp_path / "result.json").write_bytes(b"{}")
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(DomainError) as refused:
        with verified_copy(tmp_path, "result.json", sha(b"{}"), ServicePolicy()):
            pass
    assert refused.value.status == 507
    assert refused.value.details["additional_bytes"] == 2
    assert (tmp_path / "result.json").read_bytes() == b"{}"


@pytest.mark.parametrize("daily_jobs", [{"file_bytes": 128 * 1024 * 1024}], indirect=True)
def test_legal_large_result_all_read_paths_and_small_analysis_allocation(daily_jobs):
    app, jobs, body = daily_jobs
    body.update(securities=[], end_date=body["start_date"], source="""from jqdata import *
def initialize(context):
    log.info('x' * (65 * 1024 * 1024))
""")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        job_id = job["job_id"]
        root = jobs.directory(job_id)
        digest, size = evidence_hash(root, "published.json")
        assert size > 65 * 1024 * 1024
        for prefix in ("python", "history"):
            response = client.get(f"/api/v1/{prefix}/jobs/{job_id}/result")
            assert response.status_code == 200, response.text[:500]
            assert sha(response.content) == digest
            del response
            analysis = client.get(f"/api/v1/{prefix}/jobs/{job_id}/analysis")
            assert analysis.status_code == 200, analysis.text
            assert analysis.json()["source_sha256"] == digest
        tracemalloc.start()
        try:
            projected = app.state.history.result_projection(job_id)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert "logs" not in projected and peak < 4 * 1024 * 1024
        listing = client.get("/api/v1/python/backtests").json()
        item = next(item for item in listing["items"] if item["job_id"] == job_id)
        assert item["result_available"] and item["result_error"] is None
        assert item["analysis_error"] is None
        assert evidence_hash(root, "published.json") == (digest, size)
    changed = create_app(str(jobs.repo.store.path), diagnostics_configuration={},
                         daily_tasks_configuration=Configuration(None, ServicePolicy(file_bytes=1024*1024)))
    with TestClient(changed) as client:
        assert client.get("/api/v1/tasks/service").json()["policy"]["file_bytes"] == 1024*1024
        response = client.get(f"/api/v1/history/jobs/{job_id}/result")
        assert response.status_code == 200 and sha(response.content) == digest
        assert client.get(f"/api/v1/history/jobs/{job_id}/analysis").status_code == 200
    assert evidence_hash(root, "published.json") == (digest, size)
