"""Retirement boundaries and cross-version history integrity in disposable state."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.paper.api import create_app as paper_app
from ashare_quant.storage.artifacts import atomic, canonical, digest, sha
from ashare_quant.tasks.worker import work_one

RETIRED = [
    ("GET", "/api/v1/scenarios"), ("GET", "/api/v1/rqalpha/scenarios"),
    ("POST", "/api/v1/strategies"), ("POST", "/api/v1/runs"),
    ("POST", "/api/v1/data/admission"),
    ("GET", "/api/v1/tasks/daily/jobs/catalogue"),
    ("POST", "/api/v1/tasks/daily/data/bind"),
    ("POST", "/api/v1/tasks/daily/strategies"),
    ("POST", "/api/v1/tasks/daily/jobs"),
    ("GET", "/api/v1/portfolio/catalogue"),
    ("POST", "/api/v1/portfolio/strategies"),
    ("POST", "/api/v1/portfolio/preflight"), ("POST", "/api/v1/portfolio/jobs"),
]


def test_product_has_no_demo_admission_or_predefined_strategy_catalogue(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    with TestClient(app) as client:
        schema = client.get("/api/v1/openapi.json").json()
        for method, path in RETIRED:
            assert method.lower() not in schema["paths"].get(path, {})
            assert client.request(method, path, json={}).status_code in {404, 405}
        for path in ("/api/v1/python/jobs", "/api/v1/factors/jobs", "/api/v1/product/prices", "/api/v1/product/releases"):
            assert "post" in schema["paths"][path]
        assert not hasattr(app.state, "portfolio_jobs")
        assert app.state.tasks.repo.rows() == []
        assert app.state.store.strategies() == []
        assert client.get("/api/v1/history/strategies").json() == {"items": []}
        assert client.get("/api/v1/runs").json() == []


def test_python_catalogue_contains_only_capabilities(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    monkeypatch.setattr(app.state.python_jobs, "environment", lambda: "a" * 64)
    monkeypatch.setattr(app.state.python_jobs.product, "call", lambda operation: None)
    monkeypatch.setattr(app.state.python_jobs.product, "configured", lambda: SimpleNamespace(
        document={"profiles": {"product": {}}}, profile="product"))
    with TestClient(app) as client:
        value = client.get("/api/v1/python/catalogue").json()
        assert value["frequencies"] == ["1m", "1d"]
        assert "example" not in json.dumps(value)
        assert "parameters" not in value and "parameters" not in value["daily"]
        assert "windows" not in value and "algorithms" not in value


def test_paper_api_only_reads_existing_accounts(tmp_path):
    with TestClient(paper_app(tmp_path / "paper")) as client:
        schema = client.get("/openapi.json").json()
        assert all("post" not in value for value in schema["paths"].values())
        assert client.post("/api/v1/paper/sessions", json={}).status_code == 405
        assert client.get("/api/v1/paper/sessions").json()["sessions"] == []
        assert client.get("/api/v1/paper/capabilities").json()["read_only"]


def seed_history(app, family, *, status="succeeded"):
    """A constructed seal tests storage, without executing a retired algorithm."""
    schema = {"daily": "m2-job-manifest.v6", "portfolio": "portfolio-job-manifest.v1"}[family]
    job_id = "m2job-" + "a" * 32
    session = {"run_id": "a" * 32}
    request = {"name": "Preserved user research", "idempotency_key": "prior-run"}
    manifest = {"schema_version": schema, "job_id": job_id, "request": request,
                "session": session, "session_sha256": digest(session),
                "session_file_sha256": digest(session),
                "service_identity": {"source_sha256": "b" * 64}}
    root = app.state.history.directory(job_id)
    root.mkdir()
    atomic(root / "accepted-manifest.json", manifest)
    atomic(root / "session.json", session)
    value = {"job_id": job_id, "summary": {"final_equity": "100123.45"}, "source": "old-runtime"}
    evidence = {"manifest.json": canonical(manifest), "session.json": canonical(session),
                "result.json": canonical(value), "receipt.json": b"{}", "proof.json": b"{}",
                "native.chk": b"sealed-checkpoint"}
    sealed = root / "publication-1"
    sealed.mkdir()
    for name, raw in evidence.items():
        atomic(sealed / name, raw)
    seal = {"job_id": job_id, "manifest_sha256": digest(manifest),
            "files": {name: {"sha256": sha(raw), "size": len(raw)} for name, raw in evidence.items()}}
    atomic(sealed / "seal.json", seal)
    reference = dict(result_id=job_id, href="/old/endpoint", snapshot_sha256=digest(value),
                     evidence_manifest_sha256=digest(seal), publication="publication-1")
    with app.state.store.connection(transaction=True) as db:
        db.execute("INSERT INTO tasks(job_id,idempotency_key,request_hash,request,manifest,manifest_sha256,account_id,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, "prior-run", digest(request), canonical(request).decode(), canonical(manifest).decode(),
                    digest(manifest), "bt-" + "a" * 32, status, "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z",
                    canonical(reference).decode() if status == "succeeded" else None))
        db.execute("INSERT INTO registered_strategies VALUES (?, ?)",
                   ("c" * 64, canonical({"name": "User's retained strategy", "source": "user bytes"}).decode()))
    return job_id, root, value


@pytest.mark.parametrize("family", ["daily", "portfolio"])
def test_history_survives_source_change_and_checks_sealed_result(tmp_path, family):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    job_id, root, expected = seed_history(app, family)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with TestClient(app) as client:
        listing = client.get("/api/v1/history/jobs", params={"family": family}).json()["items"]
        assert len(listing) == 1 and listing[0]["read_only"] and not listing[0]["can_resume"]
        assert client.get(f"/api/v1/history/jobs/{job_id}/result").json() == expected
        assert client.get("/api/v1/history/strategies").json()["items"][0]["frozen"]["source"] == "user bytes"
        assert client.post(f"/api/v1/history/jobs/{job_id}/resume", json={}).status_code == 404
    refreshed = create_app(str(app.state.store.path), diagnostics_configuration={})
    with TestClient(refreshed) as client:
        assert client.get(f"/api/v1/history/jobs/{job_id}/result").json() == expected
        assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        (root / "publication-1/receipt.json").write_bytes(b'{"changed":true}')
        response = client.get(f"/api/v1/history/jobs/{job_id}/result")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "HISTORY_EVIDENCE_CONFLICT"


def test_worker_never_claims_or_mutates_retired_pending_work(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    job_id, root, _ = seed_history(app, "portfolio", status="queued")
    value = app.state.history.get(job_id)
    assert value["status"] == value["display_status"] == "retired"
    assert value["stored_status"] == "queued"
    assert value["execution_reason"]["code"] == "TASK_FAMILY_RETIRED"
    before = app.state.tasks.repo.row(job_id)
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert work_one(app.state.tasks) is None
    with pytest.raises(DomainError, match="read-only history"):
        work_one(app.state.tasks, job_id)
    assert app.state.tasks.repo.row(job_id) == before
    assert files == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_runtime_sources_cannot_import_development_material():
    import ast

    import ashare_quant

    root = Path(ashare_quant.__file__).parent
    assert not (root / "fixtures").exists()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            imports = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [n.name for n in node.names] if isinstance(node, ast.Import) else [])
            assert not any(m.startswith(("tests.", "examples.", "scripts.")) for m in imports), path


def test_history_preserves_separately_registered_python_source(tmp_path):
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    seed_history(app, "daily")
    source = b"raise RuntimeError('must never execute while reading history')\n"
    frozen = {"name": "User Python", "source_sha256": sha(source)}
    with app.state.store.connection(transaction=True) as db:
        db.execute("UPDATE registered_strategies SET body=?", (canonical(frozen).decode(),))
        db.execute("INSERT INTO python_sources VALUES (?,?)", ("c" * 64, source))
    with TestClient(app) as client:
        response = client.get("/api/v1/history/strategies")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["frozen"] == frozen and item["source"] == source.decode() and item["read_only"]
        with app.state.store.connection(transaction=True) as db:
            db.execute("UPDATE python_sources SET source=?", (b"altered",))
        response = client.get("/api/v1/history/strategies")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "HISTORY_SOURCE_CONFLICT"
