import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_quant.deployment.runtime import deployment_lock
from ashare_quant.strategies.imports import migrate_source, validate_imports
from ashare_quant.strategies.migrate_imports import prepare, publish, verify


@pytest.mark.parametrize("before,after", [
    (b"from jqdata import *\n", b"from aquant import *\n"),
    (b"import jqdata\njqdata.record(x=1)\n", b"import aquant as jqdata\njqdata.record(x=1)\n"),
    (b"import os, jqdata as api, sys\n", b"import os, aquant as api, sys\n"),
    (b"from jqdata import (\n    order as buy,\n    g,\n)\n", b"from aquant import (\n    order as buy,\n    g,\n)\n"),
    (b"\xef\xbb\xbffrom jqdata import g\r\n# jqdata\r\n", b"\xef\xbb\xbffrom aquant import g\r\n# jqdata\r\n"),
    ('名字 = 1; import jqdata as api\n'.encode(), '名字 = 1; import aquant as api\n'.encode()),
    (b"from aquant import *\n", b"from aquant import *\n"),
    (b"__import__('jqdatasdk')\n", b"__import__('jqdatasdk')\n"),
    (b"import jqdatasdk\n# jqdata is upstream\n", b"import jqdatasdk\n# jqdata is upstream\n"),
])
def test_import_edits_preserve_bytes_and_business_ast(before, after):
    actual, report = migrate_source(before)
    assert actual == after and report["ast_import_only"]
    repeated, repeated_report = migrate_source(after)
    assert repeated == after and repeated_report["status"] == "unchanged"
    validate_imports(actual)


@pytest.mark.parametrize("source", [
    "import jqdata.finance", "from jqdata.finance import x",
    "importlib.import_module('jqdata')", "__import__('jqdata')", "jqdata.record(x=1)",
    "exec('from jqdata import *')", "__import__('jq' + 'data')",
    "importlib.import_module(module_name)",
])
def test_ambiguous_imports_require_review_without_changing_any_bytes(source):
    raw = source.encode()
    migrated, report = migrate_source(raw)
    assert migrated == raw and report["status"] == "review_required"


@pytest.fixture
def saved(tmp_path):
    state = tmp_path / "state"
    (state / "strategies/nested").mkdir(parents=True)
    (state / "strategies/nested/a.py").write_bytes(b"from jqdata import *\r\n")
    (state / "frozen").mkdir()
    (state / "frozen/strategy.py").write_bytes(b"from jqdata import *\n")
    db = state / "control.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks(status TEXT)")
    bundle = tmp_path / "migration"
    prepare(state, bundle)
    return state, bundle, db


def test_prepare_verify_apply_resume_rollback_leaves_frozen_source_untouched(saved):
    state, bundle, db = saved
    target = state / "strategies/nested/a.py"
    assert target.read_bytes() == b"from jqdata import *\r\n"
    assert verify(bundle)[1][0]["state"] == "original"
    for _ in range(2):
        publish(bundle, control_db=db, maintenance=True)
        assert target.read_bytes() == b"from aquant import *\r\n"
    assert verify(bundle)[1][0]["state"] == "migrated"
    for _ in range(2):
        publish(bundle, rollback=True, control_db=db, maintenance=True)
    assert target.read_bytes() == b"from jqdata import *\r\n"
    assert (state / "frozen/strategy.py").read_bytes() == b"from jqdata import *\n"


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_active_tasks_block_all_writes(saved, status):
    state, bundle, db = saved
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks VALUES(?)", [status])
    with pytest.raises(ValueError, match="Unfinished"):
        publish(bundle, control_db=db, maintenance=True)
    assert verify(bundle)[1][0]["state"] == "original"


def test_live_deployment_lock_and_missing_maintenance_block(saved):
    _, bundle, db = saved
    with pytest.raises(ValueError, match="Stop API"):
        publish(bundle, control_db=db)
    with deployment_lock(SimpleNamespace(control_db=db)):
        with pytest.raises(Exception, match="Stop this deployment"):
            publish(bundle, control_db=db, maintenance=True)


@pytest.mark.parametrize("tamper", ["candidate", "source", "manifest", "symlink", "inventory"])
def test_tampered_or_changed_inputs_fail_closed(saved, tamper):
    state, bundle, db = saved
    if tamper == "candidate":
        (bundle / "candidate/nested/a.py").write_text("order('600000.XSHG', 100)")
    elif tamper == "source":
        (state / "strategies/nested/a.py").write_text("# new edit")
    elif tamper == "manifest":
        path = bundle / "manifest.json"
        data = json.loads(path.read_text())
        data["files"][0]["path"] = "../frozen/strategy.py"
        path.write_text(json.dumps(data))
    elif tamper == "symlink":
        path = bundle / "candidate/nested/a.py"
        path.unlink()
        path.symlink_to(state / "frozen/strategy.py")
    else:
        (state / "strategies/new.py").write_text("# new file")
    before = {p: p.read_bytes() for p in (state / "strategies").rglob("*.py")}
    with pytest.raises(Exception):
        publish(bundle, control_db=db, maintenance=True)
    assert {p: p.read_bytes() for p in before} == before


def test_joinquant_reference_script_remains_frozen_and_generator_selects_platform():
    import hashlib

    from tests.fixtures.minute_reference.build_scripts import generate

    probe = Path(__file__).parent / "fixtures/minute_reference/gap_probe.py"
    assert hashlib.sha256(probe.read_bytes()).hexdigest() == (
        "15fd3ba5759bd5d05915aba7edd53569986c2643a64a2e1067afd33e926ea6e0"
    )
    assert "from aquant import *" in generate("visible_momentum", "aq")
    assert "from jqdata import *" in generate("visible_momentum", "jq")
