import hashlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError, RunCreate, StrategyCreate
from ashare_quant.engine import FixtureReplayAdapter, fixture_bytes, specifications
from ashare_quant.service import Service
from ashare_quant.store import Store
from ashare_quant.worker import work_one


def engine(scenario):
    return FixtureReplayAdapter(scenario, hashlib.sha256(fixture_bytes(scenario)).hexdigest())


def expire(store, run_id):
    with store.connection(transaction=True) as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE run_id=?", (run_id,))


def test_immutable_strategy_and_request_idempotency(store, queued):
    first = queued()
    assert queued().run_id == first.run_id
    service = Service(store)
    version = service.register_strategy(StrategyCreate(name="Changed", scenario="buy_hold"))
    assert version.version_id != first.strategy_version_id
    with pytest.raises(DomainError, match="another request"):
        service.submit(RunCreate(strategy_version_id=version.version_id, idempotency_key="test-1"))
    with pytest.raises(DomainError, match="already belongs"):
        queued(key="another")
    assert store.strategy(first.strategy_version_id).definition.name == "Manual spec"


@pytest.mark.parametrize("scenario", list(specifications()))
def test_fixture_transport_matches_declared_manual_final_projection(store, queued, scenario):
    """Transport consistency only: this does NOT validate trading-rule implementation."""
    run = queued(scenario)
    work_one(store, run.run_id)
    state = store.state(run.run_id)
    expected = specifications()[scenario]["expected"]
    assert state.run.status == "succeeded"
    assert state.account.cash == expected["cash"]
    assert state.account.equity == expected["equity"]
    assert state.account.frozen_cash == expected["frozen_cash"]
    assert sum(p.quantity for p in state.account.positions) == expected["quantity"]
    assert sum(p.sellable for p in state.account.positions) == expected["sellable"]
    assert len(state.fills) == expected["fill_count"]
    assert sorted(o.reason for o in state.orders if o.status == "rejected") == expected["rejection_reasons"]
    manifest = store.manifest(run.run_id)
    assert manifest.data.evidence_kind == "synthetic"
    assert not manifest.real_strategy_executed
    assert not manifest.data.requirements_met
    assert specifications()[scenario]["real_engine_validation"] == "NOT_RUN"


def test_manual_arithmetic_independent_of_trace_values():
    """Only verifies the written oracle's arithmetic, not market rules."""
    d = Decimal
    assert d("10000") - 100 * d("10") + 100 * d("11") == d("10100")
    assert d("10000") - 100 * d("10") + 100 * d("9") == d("9900")
    assert d("9000") + 100 * d("1") + 100 * d("9") == d("10000")
    assert d("500") < 100 * d("10")


def test_atomic_checkpoint_retry_and_content_conflict(store, queued):
    run = queued()
    _, token, _ = store.claim(run.run_id)
    first = engine("buy_hold").next_step(None)
    store.commit_step(run.run_id, token, first)
    store.commit_step(run.run_id, token, first)
    assert store.run(run.run_id).cursor == 1
    assert len(store.logs(run.run_id)) == 3  # queue, claim, one event
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, first.model_copy(update={"message": "different"}))
    assert err.value.code == "EVENT_CONFLICT"
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, first.model_copy(update={"sequence": 3}))
    assert err.value.code == "EVENT_GAP"
    assert store.run(run.run_id).cursor == 1


def test_expired_worker_cannot_write_after_takeover(store, queued):
    run = queued()
    _, old, _ = store.claim(run.run_id)
    first = engine("buy_hold").next_step(None)
    store.commit_step(run.run_id, old, first)
    expire(store, run.run_id)
    _, new, checkpoint = store.claim(run.run_id)
    assert checkpoint == first.checkpoint
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, old, engine("buy_hold").next_step(checkpoint))
    assert err.value.code == "STALE_WORKER"
    store.commit_step(run.run_id, new, engine("buy_hold").next_step(checkpoint))
    assert store.run(run.run_id).cursor == 2


def test_only_one_worker_claims_a_run(store, queued):
    run = queued()
    def try_claim(_):
        try:
            return Store(store.path).claim(run.run_id)[1]
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(try_claim, range(2)))
    assert outcomes.count("RUN_LEASED") == 1


def test_cancel_fences_worker_and_releases_account(store, queued):
    run = queued()
    _, token, _ = store.claim(run.run_id)
    store.cancel(run.run_id)
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, engine("buy_hold").next_step(None))
    assert err.value.code == "STALE_WORKER"
    assert queued(key="second").run_id != run.run_id


def test_duplicate_fill_id_rolled_back_with_cursor(store, queued):
    run = queued()
    _, token, _ = store.claim(run.run_id)
    adapter = engine("buy_hold")
    first = adapter.next_step(None)
    store.commit_step(run.run_id, token, first)
    second = adapter.next_step(first.checkpoint)
    store.commit_step(run.run_id, token, second)
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, second.model_copy(update={"sequence": 3}))
    assert err.value.code == "DUPLICATE_ENGINE_EVENT"
    assert store.run(run.run_id).cursor == 2
    assert len(store.state(run.run_id).fills) == 1


def test_engine_failure_persisted_and_never_labeled_success(store, queued, monkeypatch):
    run = queued()
    def broken(*_):
        raise RuntimeError("sensitive arbitrary engine details")
    monkeypatch.setattr(FixtureReplayAdapter, "next_step", broken)
    with pytest.raises(RuntimeError):
        work_one(store, run.run_id)
    assert store.run(run.run_id).status == "failed"
    assert "sensitive" not in store.run(run.run_id).error
    with pytest.raises(DomainError) as err:
        store.claim(run.run_id)
    assert err.value.code == "RUN_TERMINAL"


def test_actual_process_exit_and_new_process_recovery(store, queued):
    run = queued("restart")
    script = '''
import os,sys
from ashare_quant.store import Store
from ashare_quant.engine import FixtureReplayAdapter
s=Store(sys.argv[1]); rid=sys.argv[2]
_, token, cp=s.claim(rid)
m=s.manifest(rid); e=FixtureReplayAdapter('restart',m.fixture_sha256)
for _ in range(2):
 step=e.next_step(cp); s.commit_step(rid,token,step); cp=step.checkpoint
os._exit(91)
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(store.path), run.run_id])
    assert crashed.returncode == 91
    assert store.run(run.run_id).cursor == 2
    assert store.state(run.run_id).account.cash == "9000.00"
    expire(store, run.run_id)  # Deterministic simulated lease expiry; no 30-second test sleep.
    recovered = subprocess.run([sys.executable, "-m", "ashare_quant.cli", "--db", str(store.path),
                                "worker", "--run-id", run.run_id], capture_output=True, text=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    state = Store(store.path).state(run.run_id)
    assert state.run.status == "succeeded" and state.run.cursor == 4
    assert state.account.cash == "9000.00" and len(state.fills) == 1


def test_backup_restores_complete_projection(store, queued, tmp_path):
    run = queued()
    work_one(store, run.run_id)
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    assert Store(backup).state(run.run_id) == store.state(run.run_id)
    with pytest.raises(DomainError):
        store.backup(backup)


def test_pin_change_blocks_but_correct_environment_can_resume(store, queued, monkeypatch):
    run = queued()
    monkeypatch.setattr("ashare_quant.worker.source_sha256", lambda: "changed")
    with pytest.raises(DomainError) as err:
        work_one(store, run.run_id)
    assert err.value.code == "BACKEND_CHANGED"
    assert store.run(run.run_id).status == "queued"
    assert store.run(run.run_id).cursor == 0
    monkeypatch.undo()
    work_one(store, run.run_id)
    assert store.run(run.run_id).status == "succeeded"


def test_failure_after_event_and_checkpoint_write_rolls_back_everything(store, queued):
    run = queued()
    _, token, _ = store.claim(run.run_id)
    with store.connection() as db:
        db.executescript("""CREATE TRIGGER simulated_disk_failure BEFORE INSERT ON logs
            WHEN NEW.message LIKE 'fixture event%'
            BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_step(run.run_id, token, engine("buy_hold").next_step(None))
    assert store.run(run.run_id).cursor == 0
    assert store.state(run.run_id).account is None
    with store.connection() as db:
        assert db.execute("SELECT checkpoint FROM runs WHERE run_id=?", (run.run_id,)).fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_duplicate_company_action_rejected_before_second_projection(store, queued):
    run = queued("corporate_action")
    _, token, cp = store.claim(run.run_id)
    adapter = engine("corporate_action")
    for _ in range(5):
        step = adapter.next_step(cp)
        store.commit_step(run.run_id, token, step)
        cp = step.checkpoint
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, step.model_copy(update={"sequence": 6}))
    assert err.value.code == "DUPLICATE_ENGINE_EVENT"
    assert store.state(run.run_id).account.cash == "9100.00"
    assert store.run(run.run_id).cursor == 5


def test_clock_regression_is_rejected(store, queued):
    from datetime import datetime
    run = queued()
    _, token, _ = store.claim(run.run_id)
    first = engine("buy_hold").next_step(None)
    store.commit_step(run.run_id, token, first)
    second = engine("buy_hold").next_step(first.checkpoint)
    with pytest.raises(DomainError) as err:
        store.commit_step(run.run_id, token, second.model_copy(update={
            "logical_at": datetime.fromisoformat("2026-09-28T09:00:00+08:00")}))
    assert err.value.code == "CLOCK_REGRESSION"
    assert store.run(run.run_id).cursor == 1
