"""Ordinary v3-to-v6 compatibility checks; these fixtures contain no market data."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.cli import main
from ashare_quant.contracts import DomainError
from ashare_quant.storage.store import Store

JOB = "m2job-" + "1" * 32
CODE = "M2_DATABASE_VERSION_INCOMPATIBLE"


def files_at(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                      p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


def insert_v3(db):
    db.execute("CREATE TABLE tasks (job_id TEXT PRIMARY KEY, manifest TEXT, status TEXT)")
    db.execute("INSERT INTO tasks VALUES (?, ?, 'queued')", (JOB, json.dumps({
        "schema_version": "m2-job-manifest.v3",
        "service_identity": {"backend_version": "0.5.0.dev3", "contract_version": "m2-jobs.v3"},
    })))
    db.execute("PRAGMA user_version=3")
    db.commit()


@pytest.fixture
def old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        insert_v3(db)
    db.close()
    return path


def assert_refusal(response):
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == CODE
    assert error["details"]["detected_manifest_versions"] == ["m2-job-manifest.v3"]
    assert error["details"]["expected_manifest_version"] == "m2-job-manifest.v6"
    assert error["details"]["detected_backend_versions"] == ["0.5.0.dev3"]
    assert error["details"]["database_modified"] is False
    assert error["details"]["automatic_migration"] is False
    assert error["details"]["old_task_execution"] is False
    assert "保留旧库" in error["message"]
    assert "新的数据库路径" in error["message"]


def test_old_list_detail_and_actions_refuse_without_changes(old_database):
    before = files_at(old_database.parent)
    app = create_app(str(old_database))
    assert not hasattr(app.state, "tasks")
    with TestClient(app) as client:
        for url in ["/api/v1/tasks/daily/jobs", f"/api/v1/tasks/daily/jobs/{JOB}",
                    f"/api/v1/tasks/daily/jobs/{JOB}/logs", "/api/v1/health"]:
            assert_refusal(client.get(url))
        for url in ["/api/v1/tasks/daily/jobs", f"/api/v1/tasks/daily/jobs/{JOB}/resume",
                    f"/api/v1/tasks/daily/jobs/{JOB}/cancel"]:
            assert_refusal(client.post(url, json={}))
    assert files_at(old_database.parent) == before


@pytest.mark.parametrize("arguments", [["history"], ["task-status", JOB], ["task-worker", "--once"]])
def test_old_cli_refuses_without_changes(old_database, arguments):
    before = files_at(old_database.parent)
    result = subprocess.run([sys.executable, "-m", "ashare_quant.cli", "--db", str(old_database),
                             *arguments], text=True, capture_output=True)
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stdout)["error"]["code"] == CODE
    assert files_at(old_database.parent) == before


def test_serve_reaches_structured_http_refusal(old_database, monkeypatch):
    before = files_at(old_database.parent)
    called = []

    def run(app, **kwargs):
        with TestClient(app) as client:
            assert_refusal(client.get("/api/v1/tasks/daily/jobs"))
        called.append(kwargs)

    monkeypatch.setattr("uvicorn.run", run)
    assert main(["--db", str(old_database), "serve", "--port", "8806"]) == 0
    assert called == [{"host": "127.0.0.1", "port": 8806}]
    assert files_at(old_database.parent) == before


def test_wal_only_old_tasks_are_detected_without_touching_sidecars(tmp_path):
    path = tmp_path / "wal.sqlite3"
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        insert_v3(db)
        assert Path(str(path) + "-wal").stat().st_size > 0
        before = files_at(tmp_path)
        with TestClient(create_app(str(path))) as client:
            assert_refusal(client.get("/api/v1/tasks/daily/jobs"))
        with pytest.raises(DomainError, match="旧版") as error:
            Store(path)
        assert error.value.code == CODE
        assert files_at(tmp_path) == before
        assert db.execute("SELECT status FROM tasks").fetchone()[0] == "queued"
    finally:
        db.close()


def test_fresh_v6_database_can_reopen(tmp_path):
    path = tmp_path / "v6.sqlite3"
    for _ in range(2):
        with TestClient(create_app(str(path))) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert client.get("/api/v1/history/jobs").json() == {"items": []}
            assert client.get("/api/v1/tasks/service").status_code == 200
            assert client.get("/api/v1/tasks/daily/jobs").status_code == 404


def test_previous_table_layout_is_refused_without_creating_new_task_storage(tmp_path):
    path = tmp_path / "previous-layout.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE m2_jobs (job_id TEXT PRIMARY KEY, manifest TEXT, status TEXT)")
        db.execute("INSERT INTO m2_jobs VALUES (?, ?, 'queued')", (JOB, json.dumps({
            "schema_version": "m2-job-manifest.v6",
            "service_identity": {"backend_version": "0.11.0.dev1", "source_sha256": "1" * 64},
        })))
        db.execute("PRAGMA user_version=3")
    db.close()
    before = files_at(tmp_path)
    with TestClient(create_app(str(path))) as client:
        response = client.post(f"/api/v1/tasks/daily/jobs/{JOB}/resume", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "M2_DATABASE_SCHEMA_INCOMPATIBLE"
        assert response.json()["error"]["details"]["database_modified"] is False
    assert files_at(tmp_path) == before
    assert not path.with_name(path.name + ".tasks").exists()
