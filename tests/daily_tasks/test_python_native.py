"""Self-owned ordinary callbacks through the admitted daily native path."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.storage.store import Store
from ashare_quant.tasks.worker import work_one
from tests.daily_tasks.conftest import create
from tests.daily_tasks.test_native import complete
from tests.support.daily.executor import Configuration
from tests.support.daily.service import Jobs
from tests.support.legacy_python.spec import PythonDescriptor

EXAMPLES = Path(__file__).parents[2] / "tests/fixtures/research" / "python"


def register(jobs, file):
    return jobs.register_python(file, PythonDescriptor())


def task(client, version, *, window="two_day", cash="10000", key="python", parameters=None):
    return create(
        client,
        window,
        key,
        {
            "template": "registered_python.v1",
            "strategy_version_id": version.version_id,
            "initial_cash": cash,
            "strategy_parameters": parameters or {},
        },
    )


def callback_lines(root):
    return [
        line
        for p in sorted(root.glob("attempt-*.process.log"))
        for line in p.read_text().splitlines()
        if "AQ_DECISION" in line
    ]


@pytest.mark.parametrize(
    "name,window,cash,targets,balances,equity,fills",
    [
        ("hold", "two_day", "10000", [100, 100], ["8735", "8735"], "9981", 1),
        ("cash", "two_day", "1500", [100, 0], ["235", "1474.754"], "1474.754", 2),
        (
            "staged",
            "w1",
            "10000",
            [100, 200, 0] + [None] * 7,
            ["8735", "7484"] + ["9976.5"] * 8,
            "9976.5",
            3,
        ),
    ],
)
def test_python_manual_account_and_time_oracles(
    host, name, window, cash, targets, balances, equity, fills
):
    jobs, client = host
    version = register(jobs, EXAMPLES / (name + ".py"))
    job = task(client, version, window=window, cash=cash)
    result = complete(jobs, job["job_id"])
    assert [d.signal.target_shares for d in result.days] == targets
    assert [Decimal(d.account_at_day_close.cash) for d in result.days] == list(
        map(Decimal, balances)
    )
    assert Decimal(result.summary.final_equity) == Decimal(equity)
    assert result.summary.committed_fill_count == fills
    assert result.days[0].account_at_day_close.sellable == 0
    assert result.strategy == version
    assert result.data_identity.original_quality["historical_pit_verified"] is False
    assert result.data_identity.source_evidence_kind == "source_claim_unverified"
    for index, day in enumerate(result.days):
        record = day.python_decision
        assert day.decision_verification == "trusted_python_record.v1"
        assert record.source_sha256 == version.source_sha256
        assert record.input["day_index"] == index
        assert record.input["logical_at"].endswith("T09:00:00+08:00")
        assert record.input["data"]["price_date"] == day.previous_open.isoformat()
        assert day.previous_open < day.trading_date
        assert record.input["prior_account"]["snapshot_as_of"] <= record.input["logical_at"]
        assert Decimal(record.input["data"]["previous_close"]) > 0
    root = jobs.directory(job["job_id"])
    before = callback_lines(root)
    assert len(before) == len(targets)
    assert client.get(f"/api/v1/tasks/daily/jobs/{job['job_id']}/result").status_code == 200
    assert jobs.get(job["job_id"]).status == "succeeded"
    assert callback_lines(root) == before
    manifest = jobs.check(jobs.repo.row(job["job_id"]))
    proof = jobs.executor.immediate(
        "inspect", root, window, "read-only-check", manifest.session_file_sha256
    )
    assert proof["complete"]
    assert callback_lines(root) == before
    assert "AQ_DECISION" not in (root / "read-only-check.runner.log").read_text()
    assert "AQ_DECISION" not in (root / "prepare.runner.log").read_text()


def test_python_insufficient_cash_is_native_rejection(host):
    jobs, client = host
    version = register(jobs, EXAMPLES / "hold.py")
    result = complete(jobs, task(client, version, cash="1000")["job_id"])
    assert result.summary.committed_fill_count == 0
    assert Decimal(result.summary.final_cash) == Decimal("1000")
    assert any(day.native_rejections for day in result.days)


@pytest.mark.parametrize(
    "body,code",
    [
        ("return None", "PYTHON_OUTPUT_INVALID"),
        ("return TargetShares(350)", "PYTHON_OUTPUT_INVALID"),
        ('raise ValueError("owned example error")', "PYTHON_CALLBACK_ERROR"),
    ],
)
def test_python_ordinary_errors_stop_before_next_order(host, tmp_path, body, code):
    jobs, client = host
    file = tmp_path / "error.py"
    file.write_text(
        "from ashare_quant.strategies.api import TargetShares\n"
        "def on_decision(context, data, parameters):\n"
        "    if context.day_index == 0:\n        return TargetShares(100)\n"
        "    " + body + "\n"
    )
    version = register(jobs, file)
    job = task(client, version)
    work_one(jobs, job["job_id"])
    view = jobs.get(job["job_id"])
    assert view.status == "failed", view
    assert view.error["code"] == code, view.error
    assert view.error["details"]["phase"] == "CALLBACK"
    assert view.error["details"]["strategy_version_id"] == version.version_id
    if code == "PYTHON_CALLBACK_ERROR":
        assert view.error["details"]["source_line"] is not None
    assert view.result is None and view.can_resume
    root = jobs.directory(job["job_id"])
    manifest = jobs.check(jobs.repo.row(job["job_id"]))
    proof = jobs.executor.immediate(
        "inspect", root, "two_day", "error-read", manifest.session_file_sha256
    )
    assert 0 < proof["cursor"] < 6
    assert not proof["complete"]


def test_python_fresh_namespace_and_frozen_parameters(host, tmp_path):
    jobs, client = host
    file = tmp_path / "state.py"
    file.write_text(
        "from ashare_quant.strategies.api import TargetShares\n"
        'count = 0\nprint("OWNED_LOAD")\n'
        "def on_decision(context, data, parameters):\n"
        "    global count\n    count += 1\n"
        '    return TargetShares(parameters["shares"] * count)\n'
    )
    version = register(jobs, file)
    job = task(client, version, parameters={"shares": 100, "label": "own", "enabled": True})
    file.write_text("# changed editor file after acceptance\n")
    result = complete(jobs, job["job_id"])
    assert [d.signal.target_shares for d in result.days] == [100, 100]
    root = jobs.directory(job["job_id"])
    assert (root / "attempt-1.process.log").read_text().count("OWNED_LOAD") == 2
    assert "OWNED_LOAD" not in (root / "prepare.runner.log").read_text()
    assert (root / "strategy-source.py").read_bytes() != file.read_bytes()


@pytest.mark.parametrize("fault", ["after_replace:1", "native_submit:5"])
def test_python_committed_decision_recovery_and_relocation(host, tmp_path, fault):
    jobs, client = host
    file = tmp_path / "original.py"
    file.write_bytes((EXAMPLES / "staged.py").read_bytes())
    version = register(jobs, file)
    job = task(client, version, window="w1")
    work_one(jobs, job["job_id"], _fault=fault)
    assert jobs.get(job["job_id"]).status == "failed"
    file.unlink()
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("DELETE FROM python_sources WHERE version_id=?", (version.version_id,))
        db.execute("DELETE FROM registered_strategies WHERE version_id=?", (version.version_id,))
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(str(jobs.repo.store.path), moved / "v5.sqlite3")
    shutil.move(str(jobs.root), moved / "v5.sqlite3.tasks")
    shutil.copytree(jobs.config.owner_store, moved / "owner")
    resumed = Jobs(Store(moved / "v5.sqlite3"), Configuration(moved / "owner"))
    before = callback_lines(resumed.directory(job["job_id"]))
    resumed.resume(job["job_id"])
    assert callback_lines(resumed.directory(job["job_id"])) == before
    result = complete(resumed, job["job_id"])
    assert result.strategy == version
    assert Decimal(result.summary.final_cash) == Decimal("9976.5")
    assert result.summary.committed_fill_count == 3
    assert len(callback_lines(resumed.directory(job["job_id"]))) == 10
    assert len({fill.fill_id for day in result.days for fill in day.fills}) == 3


def test_python_completed_publication_retry_does_not_execute_callback(host):
    jobs, client = host
    version = register(jobs, EXAMPLES / "hold.py")
    job = task(client, version)

    def stage(name, *args):
        if name == "before_publish":
            raise RuntimeError("controlled publication interruption")

    work_one(jobs, job["job_id"], _stage=stage)
    root = jobs.directory(job["job_id"])
    before = callback_lines(root)
    assert len(before) == 2
    jobs.resume(job["job_id"])
    result = complete(jobs, job["job_id"])
    assert result.recovery["completion"]["native_run"] is False
    assert callback_lines(root) == before


def test_python_module_load_error_is_recorded_without_decision(host, tmp_path):
    jobs, client = host
    file = tmp_path / "load_error.py"
    file.write_text(
        "from ashare_quant.strategies.api import TargetShares\n"
        'raise ValueError("owned module configuration error")\n'
        "def on_decision(context, data, parameters):\n"
        "    return TargetShares(100)\n"
    )
    version = register(jobs, file)
    job = task(client, version)
    assert jobs.get(job["job_id"]).status == "queued"
    work_one(jobs, job["job_id"])
    view = jobs.get(job["job_id"])
    assert view.status == "failed" and view.error["code"] == "PYTHON_LOAD_ERROR"
    assert view.error["details"]["phase"] == "MODULE_LOAD"
    assert view.error["details"]["source_line"] == 2
    manifest = jobs.check(jobs.repo.row(job["job_id"]))
    proof = jobs.executor.immediate(
        "inspect",
        jobs.directory(job["job_id"]),
        "two_day",
        "load-error-read",
        manifest.session_file_sha256,
    )
    assert proof["cursor"] == 0 and not proof["complete"]
