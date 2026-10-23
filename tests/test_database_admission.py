"""Failure-atomic admission: never initialize, migrate or repair an existing file."""

import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import ashare_quant.storage.compatibility as database_compat
import ashare_quant.storage.lifecycle as database_lifecycle
from ashare_quant.cli import main
from ashare_quant.contracts import DomainError
from ashare_quant.storage.store import Store
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.repository import Repository
from tests.support.app import create_app


def inventory(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                      p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def refusal(path, code=None):
    with TestClient(create_app(str(path)), raise_server_exceptions=False) as client:
        result = client.get("/api/v1/health")
    assert result.status_code == 409, result.text
    if code:
        assert result.json()["error"]["code"] == code
    return result.json()


@pytest.mark.parametrize("table", ["python_sources", "strategies", "runs", "events", "task_logs"])
def test_corrupt_non_manifest_btree_refuses_without_touching_original(tmp_path, table):
    path = tmp_path / "db.sqlite3"
    create_app(str(path))
    with sqlite3.connect(path) as db:
        page = db.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        size = db.execute("PRAGMA page_size").fetchone()[0]
    db.close()
    with path.open("r+b") as stream:
        stream.seek((page - 1) * size)
        stream.write(b"\xff")
    before = inventory(tmp_path)
    refusal(path)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("name", ["tasks", "task_logs", "task_service", "python_sources", "runs", "strategies"])
def test_reserved_views_cannot_bypass_preflight(tmp_path, name):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE historical_tasks(manifest TEXT)")
        db.execute("INSERT INTO historical_tasks VALUES(?)", (json.dumps({"schema_version": "m2-job-manifest.v3"}),))
        db.execute(f"CREATE VIEW {name} AS SELECT * FROM historical_tasks")
        db.execute("PRAGMA user_version=3")
    db.close()
    before = inventory(tmp_path)
    refusal(path, "M2_DATABASE_SCHEMA_INCOMPATIBLE")
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("change", [
    "DROP INDEX engine_fill_once",
    "DROP INDEX tasks_queue; CREATE INDEX tasks_queue ON tasks(account_id)",
    "DROP TABLE task_service",
    "ALTER TABLE tasks ADD COLUMN unchecked TEXT",
    "PRAGMA user_version=1", "PRAGMA user_version=2",
])
def test_unsupported_schema_is_not_silently_repaired(tmp_path, change):
    path = tmp_path / "control.sqlite3"
    create_app(str(path))
    with sqlite3.connect(path) as db:
        db.executescript(change)
    db.close()
    before = inventory(tmp_path)
    refusal(path, "M2_DATABASE_SCHEMA_INCOMPATIBLE")
    assert inventory(tmp_path) == before


def test_existing_empty_file_is_preserved(tmp_path):
    path = tmp_path / "empty.sqlite3"
    path.touch()
    before = inventory(tmp_path)
    refusal(path, "M2_DATABASE_SCHEMA_INCOMPATIBLE")
    assert inventory(tmp_path) == before


def test_reopen_with_fixed_policy_never_opens_original_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    create_app(str(path))
    before = inventory(tmp_path)
    connect = sqlite3.connect

    def copies_only(database, *args, **kwargs):
        assert str(database) not in {str(path), path.as_uri() + "?mode=rw"}
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", copies_only)
    with TestClient(create_app(str(path))) as client:
        assert client.get("/api/v1/health").status_code == 200
    assert inventory(tmp_path) == before


def test_policy_refusal_does_not_write_database_or_sidecars(tmp_path):
    from tests.support.daily.executor import Configuration

    path = tmp_path / "control.sqlite3"
    create_app(str(path))
    before = inventory(tmp_path)
    app = create_app(str(path), daily_tasks_configuration=Configuration(None, ServicePolicy(max_workers=2)))
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["error"]["code"] == "M2_JOB_POLICY_CHANGED"
    assert inventory(tmp_path) == before


def test_failed_staged_ddl_never_publishes_half_initialized_database(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"
    monkeypatch.setattr(database_lifecycle, "SCHEMA", database_lifecycle.SCHEMA + "\ninvalid SQL;")
    with TestClient(create_app(str(path)), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DATABASE_OPERATION_REFUSED"
    assert not path.exists() and list(tmp_path.iterdir()) == []


def test_failed_stage_flush_never_publishes_database(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"

    def full(*_):
        raise OSError(errno.ENOSPC, "bounded fsync injection")

    monkeypatch.setattr(database_lifecycle.os, "fsync", full)
    with TestClient(create_app(str(path)), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_FULL"
    assert not path.exists() and list(tmp_path.iterdir()) == []


def test_competing_unsupported_file_is_never_overwritten_or_initialized(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"
    link = os.link
    winner = {}

    def publish(staged, target):
        assert target == path
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIEW tasks AS SELECT 'kept' AS job_id")
        db.close()
        winner.update(inventory(tmp_path))
        return link(staged, target)

    monkeypatch.setattr(database_lifecycle.os, "link", publish)
    refusal(path, "M2_DATABASE_SCHEMA_INCOMPATIBLE")
    assert inventory(tmp_path) == {path.name: winner[path.name]}


def test_change_after_snapshot_validation_is_rechecked_before_any_original_open(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    create_app(str(path))
    validate = database_compat._validate
    after_external_change = {}

    def change_after_check(db, original):
        validate(db, original)
        if not after_external_change:
            with sqlite3.connect(path) as other:
                other.execute("DROP INDEX engine_action_once")
            other.close()
            after_external_change.update(inventory(tmp_path))

    monkeypatch.setattr(database_compat, "_validate", change_after_check)
    refusal(path, "M2_DATABASE_SCHEMA_INCOMPATIBLE")
    assert inventory(tmp_path) == after_external_change


def test_initialization_excludes_live_store_transaction_until_check_finishes(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    store = Store(path)
    Repository(store)
    with store.connection(transaction=True) as db:
        db.execute("CREATE TABLE marker(value INTEGER)")
    validate = database_compat._validate
    checking, release, writer_entered, committed = [threading.Event() for _ in range(4)]

    def paused(db, original):
        validate(db, original)
        checking.set()
        assert release.wait(5)

    def writer():
        writer_entered.set()
        with store.connection(transaction=True) as db:
            db.execute("INSERT INTO marker VALUES(1)")
        committed.set()

    monkeypatch.setattr(database_compat, "_validate", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        opener = pool.submit(Store, path)
        try:
            assert checking.wait(3)
            future = pool.submit(writer)
            assert writer_entered.wait(3)
            assert not committed.wait(0.1)
        finally:
            release.set()
        opener.result(timeout=5)
        future.result(timeout=5)
    with store.connection() as db:
        assert db.execute("SELECT value FROM marker").fetchall()[0][0] == 1
    assert committed.is_set()


def test_concurrent_processes_admit_one_immutable_policy(tmp_path):
    path = tmp_path / "control.sqlite3"
    gate = tmp_path / "go"
    script = r'''
import json,sys,time
from pathlib import Path
from ashare_quant.contracts import DomainError
from ashare_quant.storage.store import Store
from ashare_quant.tasks.repository import Repository
from ashare_quant.tasks.policy import ServicePolicy
while not Path(sys.argv[3]).exists(): time.sleep(.01)
try:
    repo=Repository(Store(sys.argv[1]),ServicePolicy(max_workers=int(sys.argv[2])))
    print(json.dumps({'accepted':repo.policy.max_workers}))
except DomainError as exc: print(json.dumps({'error':exc.code}))
'''
    processes = [subprocess.Popen([sys.executable, "-B", "-c", script, str(path), str(n), str(gate)],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in (1, 2)]
    try:
        gate.touch()
        results = []
        for process in processes:
            out, err = process.communicate(timeout=12)
            assert process.returncode == 0, err
            results.append(json.loads(out))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    assert sum("accepted" in r for r in results) == 1
    assert {r.get("error") for r in results} == {None, "M2_JOB_POLICY_CHANGED"}
    policy = Repository(Store(path)).policy.max_workers
    assert {r["accepted"] for r in results if "accepted" in r} == {policy}


@pytest.mark.parametrize("replacement", [False, True])
def test_existing_store_refuses_removed_or_replaced_database(tmp_path, replacement):
    store = Store(tmp_path / "control.sqlite3")
    if replacement:
        other = Store(tmp_path / "other.sqlite3")
        os.replace(other.path, store.path)
    else:
        store.path.unlink()
    before = inventory(tmp_path)
    with pytest.raises(DomainError) as error, store.connection(transaction=True):
        pytest.fail("A stale handle must not recreate or write the new file")
    assert error.value.code == "DATABASE_FILE_CHANGED"
    assert inventory(tmp_path) == before


def test_read_only_initialization_directory_returns_http_and_cli_code(tmp_path, capsys):
    path = tmp_path / "control.sqlite3"
    tmp_path.chmod(0o500)
    try:
        with TestClient(create_app(str(path)), raise_server_exceptions=False) as client:
            response = client.get("/api/v1/health")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_PERMISSION_DENIED"
        assert main(["--db", str(path), "history"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "STORAGE_PERMISSION_DENIED"
    finally:
        tmp_path.chmod(0o700)
    assert not path.exists()
