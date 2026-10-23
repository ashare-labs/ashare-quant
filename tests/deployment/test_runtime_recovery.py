"""Portable state means files, native money/trades and continuation, not just SQL."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from ashare_quant.contracts import DomainError, RunCreate, StrategyCreate
from ashare_quant.deployment import resolve
from ashare_quant.deployment.backup import create_backup, restore_backup, verify_backup
from ashare_quant.deployment.runtime import commands, deployment_lock, doctor, initialize
from ashare_quant.paper.service import PaperService
from ashare_quant.storage.artifacts import lock
from ashare_quant.storage.store import Store
from ashare_quant.strategies.files import StrategyFiles
from tests.paper.fixtures import config as paper_config
from tests.paper.fixtures import queue
from tests.paper.test_recovery import economics
from tests.support.synthetic.service import Service
from tests.support.synthetic.worker import work_one


def manifest(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "deployment.json"
    document = {
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "main", "role": "market"},
            "releases": {"kind": "store", "path": "releases", "role": "publication"},
            "inputs": {"kind": "directory", "path": "external-inputs", "role": "input"},
            "frozen": {"kind": "reference", "path": "never-open", "role": "protected_external"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
            "control_db": "state/control.sqlite3", "paper_root": "paper",
            "required_assets": ["inputs"],
        }},
    }
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def deployment(tmp_path):
    path = manifest(tmp_path / "live")
    config = resolve(path)
    config.daily_root.mkdir()
    (config.daily_root / "writer.lock").touch()
    with closing(sqlite3.connect(config.daily_root / "daily.sqlite3")) as db:
        for name in ("bars", "instruments", "calendar", "responses"):
            db.execute(f"CREATE TABLE {name}(value TEXT)")
    inputs = config.asset("inputs").path
    inputs.mkdir()
    (inputs / "owner-input.json").write_text('{"source":"/original/owner/source.json","value":"真实原文"}')
    initialize(config)
    return config


def queued(config, key="restored-pending"):
    store = Store(config.control_db)
    service = Service(store)
    strategy = service.register_strategy(StrategyCreate(name="recovery", scenario="buy_hold"))
    return service.submit(RunCreate(strategy_version_id=strategy.version_id, idempotency_key=key))


def test_doctor_and_commands_are_read_only_and_share_profile(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(path))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    config = resolve()
    result = doctor(config)
    assert result["ready"] is False
    assert not config.control_db.exists()
    assert commands(config)["api"][:-1] == commands(config)["task_worker"][:-1]
    assert (config.api_port, config.paper_port, config.web_port) == (18839, 18818, 41839)
    assert commands(config)["web"]["environment"]["ASHARE_API_URL"] == "http://127.0.0.1:18839"
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    monkeypatch.delenv("ASHARE_DEPLOYMENT")
    monkeypatch.setenv("ASHARE_DATA_ROOT", str(tmp_path))
    assert resolve() == config


@pytest.mark.parametrize("field,value", [("api_port", 18818), ("default_dataset_id", "not-a-dataset"),
                                        ("control_db", "main/control.sqlite3")])
def test_invalid_configuration_is_refused(tmp_path, field, value):
    path = manifest(tmp_path)
    document = json.loads(path.read_text())
    document["profiles"]["product"][field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(DomainError) as error:
        resolve(path)
    assert error.value.code == "DEPLOYMENT_INVALID"


def test_native_money_trades_and_pending_run_survive_clean_restore(deployment, tmp_path):
    paper = PaperService(deployment.paper_root)
    session = paper_config("bundle-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16])
    sid = paper.create(session)["id"]
    first_day = queue(paper, sid, session, 0, (100, 0))
    first = paper.work(sid, first_day)
    assert first["native"]["trades"] and first["native"]["account"]["positions"]
    second_day = queue(paper, sid, session, 1, (0, 100), first["checkpoint_sha256"])
    pending = queued(deployment)
    strategies = StrategyFiles(deployment.control_db.parent)
    strategies.mkdir("研究")
    strategies.save("研究/saved.py", "# 用户源码\n")
    bundle = tmp_path / "backup"
    result = create_backup(deployment, bundle)
    assert result["scope"] == "state"
    assert not any("daily.sqlite" in f for f in verify_backup(bundle)["files"])
    expected = economics(paper.work(sid, second_day))
    original_store = Store(deployment.control_db)
    work_one(original_store, pending.run_id)
    expected_run = original_store.state(pending.run_id)
    restored = restore_backup(bundle, tmp_path / "restored", expected_sha256=result["manifest_sha256"])
    config = resolve(restored["deployment"])
    # Neither continued execution nor preserved reports may depend on the old root.
    deployment.data_root.rename(tmp_path / "old-root-hidden")
    assert StrategyFiles(config.control_db.parent).read("研究/saved.py")["source"] == "# 用户源码\n"
    restored_paper = PaperService(config.paper_root)
    assert economics(restored_paper.report(sid, first_day)) == economics(first)
    assert economics(restored_paper.work(sid, second_day)) == expected
    store = Store(config.control_db)
    work_one(store, pending.run_id)
    actual = store.state(pending.run_id)
    assert actual.account == expected_run.account and actual.fills == expected_run.fills
    assert len(actual.fills) > 0
    source_bytes = (config.asset("inputs").path / "owner-input.json").read_bytes()
    assert b"/original/owner/source.json" in source_bytes
    assert not config.daily_root.exists()  # State continuation does not invent a source database.


def test_source_backup_captures_sqlite_wal_and_raw_inputs(deployment, tmp_path):
    db = sqlite3.connect(deployment.daily_root / "daily.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE observations(price TEXT)")
        db.execute("INSERT INTO observations VALUES('12.75')")
        db.commit()
        (deployment.daily_root / "raw-receipt.json").write_text('{"origin":"verbatim"}')
        result = create_backup(deployment, tmp_path / "full", include_source_assets=True)
        assert result["scope"] == "source-assets-and-state"
        restored = restore_backup(tmp_path / "full", tmp_path / "restored")
        config = resolve(restored["deployment"])
        with closing(sqlite3.connect(config.daily_root / "daily.sqlite3")) as recovered:
            assert recovered.execute("SELECT price FROM observations").fetchall() == [("12.75",)]
        assert (config.daily_root / "raw-receipt.json").read_text() == '{"origin":"verbatim"}'
        assert any(x["name"] == "frozen" and x["reason"] == "protected_external"
                   for x in result["omitted_assets"])
    finally:
        db.close()


@pytest.mark.parametrize("active", ["runtime", "writer", "run"])
def test_backup_refuses_active_writers(deployment, tmp_path, active):
    if active == "runtime":
        context = deployment_lock(deployment)
    elif active == "writer":
        context = lock(deployment.paper_root / "writer.lock")
    else:
        run = queued(deployment)
        Store(deployment.control_db).claim(run.run_id)
        from contextlib import nullcontext
        context = nullcontext()
    with context, pytest.raises(DomainError) as error:
        create_backup(deployment, tmp_path / "refused")
    assert error.value.code in {"DEPLOYMENT_BUSY", "BACKUP_WRITER_BUSY", "BACKUP_ACTIVE_TASKS"}
    assert not (tmp_path / "refused").exists()


def test_backup_refuses_changing_external_input(deployment, tmp_path, monkeypatch):
    import ashare_quant.deployment.backup as backup

    snapshot = backup._snapshot
    def changed(source, destination):
        snapshot(source, destination)
        (deployment.asset("inputs").path / "owner-input.json").write_text("changed")
    monkeypatch.setattr(backup, "_snapshot", changed)
    with pytest.raises(DomainError) as error:
        create_backup(deployment, tmp_path / "refused")
    assert error.value.code == "BACKUP_SOURCE_CHANGED"


@pytest.mark.parametrize("mutation", ["data", "extra", "path", "runtime"])
def test_restore_rejects_tampering_before_writing(deployment, tmp_path, monkeypatch, mutation):
    import ashare_quant.deployment.backup as backup

    bundle = tmp_path / "backup"
    create_backup(deployment, bundle)
    if mutation == "data":
        (bundle / "payload/state/control.sqlite3").write_bytes(b"tamper")
    elif mutation == "extra":
        (bundle / "extra.txt").write_text("unlisted")
    elif mutation == "path":
        path = bundle / "manifest.json"
        document = json.loads(path.read_text())
        document["files"]["../escape"] = {"size": 0, "sha256": "0" * 64}
        path.write_text(json.dumps(document))
        (bundle / "manifest.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        monkeypatch.setattr(backup, "runtime_identity", lambda: {"different": True})
    with pytest.raises(DomainError) as error:
        restore_backup(bundle, tmp_path / "not-created")
    assert error.value.code.startswith("BACKUP_")
    assert not (tmp_path / "not-created").exists()


def test_symlink_and_nonempty_restore_refused(deployment, tmp_path):
    alias = deployment.asset("inputs").path / "linked"
    alias.symlink_to(deployment.control_db)
    with pytest.raises(DomainError) as error:
        create_backup(deployment, tmp_path / "refused")
    assert error.value.code == "BACKUP_UNSUPPORTED_FILE"
    alias.unlink()
    create_backup(deployment, tmp_path / "backup")
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep").write_text("user file")
    with pytest.raises(DomainError) as error:
        restore_backup(tmp_path / "backup", target)
    assert error.value.code == "RESTORE_DESTINATION_NOT_EMPTY"
    assert (target / "keep").read_text() == "user file"


def test_cli_doctor_does_not_initialize(tmp_path):
    path = manifest(tmp_path)
    result = subprocess.run([sys.executable, "-m", "ashare_quant.deployment", "--deployment",
                             str(path), "doctor"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["missing"]
    assert not (tmp_path / "state").exists()


def test_init_refuses_empty_source_before_creating_state(tmp_path):
    path = manifest(tmp_path)
    config = resolve(path)
    config.daily_root.mkdir()
    with pytest.raises(DomainError) as error:
        initialize(config)
    assert error.value.code == "DEPLOYMENT_SOURCE_MISSING"
    assert not config.control_db.exists() and not config.paper_root.exists()
    assert not doctor(config)["ready"]
