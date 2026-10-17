"""Full Python migration and failure cases; synthetic data is engineering only."""

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ashare_quant.backtests.minute import run_backtest
from ashare_quant.backtests.minute.runner import MinuteRunError
from tests.fixtures.minute_reference.audit_execution import audit_run

ROOT = Path(__file__).resolve().parents[2]
JQ = dict(
    lifecycle="jq_minute",
    matching="jq_current",
    capacity_policy="jq_daily_initial",
    volume_percent=0.0001,
    initial_cash=10000000,
    sell_tax=0.0005,
)


def messages(events):
    return [
        json.loads(e["message"].split(" ", 1)[1])
        for e in events
        if e["kind"] == "log" and e["message"].startswith("AQGAP_JSON ")
    ]


@pytest.fixture
def gap_run(run_minute, minute_warmup_doc):
    source = (ROOT / "tests/fixtures/minute_reference/gap_ported.py").read_text()
    return run_minute(source, doc=minute_warmup_doc, config=JQ)


def test_complete_real_probe_source_with_one_explicit_raw_price_argument(gap_run):
    original = (ROOT / "tests/fixtures/minute_reference/gap_probe.py").read_text()
    ported = (ROOT / "tests/fixtures/minute_reference/gap_ported.py").read_text()
    assert hashlib.sha256(original.encode()).hexdigest() == (
        "15fd3ba5759bd5d05915aba7edd53569986c2643a64a2e1067afd33e926ea6e0"
    )
    assert ported == original.replace(
        "skip_paused=False, df=True)", "skip_paused=False, df=True, fq=None)"
    )
    result, events, root = gap_run
    rows = messages(events)
    assert rows[0]["kind"] == "identity" and rows[-1]["kind"] == "complete"
    assert rows[-1]["days"] == 3
    assert not result["real_market_data"] and result["jq_comparison"] == "NOT_PERFORMED"
    assert not any(r["kind"].endswith("error") for r in rows)
    for row in rows:
        if row["kind"] == "market":
            assert all("history_error" not in v for v in row["securities"].values())
    assert len(result["orders"]) == 5 and len(result["trades"]) == 11
    report = audit_run(root)
    assert report["both_cash_components_checked"] and report["observer_snapshots_checked"] >= 48
    assert len(report["first_trade_observations"]) == 11
    first = next(r for r in rows if r["kind"] == "market")
    assert {h["label"][:10] for v in first["securities"].values() for h in v["history"]} == {
        "2024-03-01"
    }
    assert not any(e["at"].startswith("2024-03-01") for e in events)
    assert sum(e["kind"] == "clock" and e["phase"] == "bar" for e in events) == 720


def test_new_policy_observer_cash_components_cannot_offset_each_other(gap_run, tmp_path):
    _, events, root = gap_run
    changed = copy.deepcopy(events)
    for event in changed:
        if event["kind"] != "log" or not event["message"].startswith("AQGAP_JSON "):
            continue
        row = json.loads(event["message"].split(" ", 1)[1])
        if row["kind"] == "account" and row["account"]["locked_cash"] > 0:
            row["account"]["available_cash"] -= 100
            row["account"]["locked_cash"] += 100
            event["message"] = "AQGAP_JSON " + json.dumps(row)
            break
    else:
        raise AssertionError("A binding pending order is required")
    target = tmp_path / "cash-mutant"
    shutil.copytree(root, target)
    path = target / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in changed))
    receipt = json.loads((target / "complete.json").read_text())
    receipt["artifacts"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (target / "complete.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="frozen cash mismatch"):
        audit_run(target)


def test_policy_identity_reentry_and_terminal_failure(
    gap_run, tmp_path, run_minute, minute_session_doc
):
    _, _, root = gap_run
    again = run_backtest(root / "strategy.py", root / "input.json", root, config=JQ)
    assert again["native_run"] is False
    with pytest.raises(ValueError, match="COMPLETED_IDENTITY_MISMATCH"):
        run_backtest(
            root / "strategy.py",
            root / "input.json",
            root,
            config={**JQ, "capacity_policy": "strict_minute"},
        )
    source = (
        "def initialize(c): pass\ndef on_strategy_end(c): raise RuntimeError('terminal-marker')"
    )
    with pytest.raises(MinuteRunError, match="terminal-marker"):
        run_minute(source, doc=minute_session_doc, config=JQ, name="terminal-failure")
    assert not (tmp_path / "terminal-failure/complete.json").exists()
    assert (tmp_path / "terminal-failure/failure.json").exists()


def test_odd_share_t1_exit_and_fee_carry_remain_native(run_minute, minute_session_doc):
    source = """from jqdata import *
def initialize(c):
    g.day=0
    run_daily(buy,'09:31'); run_daily(cancel,'09:33'); run_daily(sell,'09:34')
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day==1: g.buy=order('600000.XSHG',1000,LimitOrderStyle(10.5))
def cancel(c):
    if g.day==1:
        assert get_orders(g.buy.order_id)[g.buy.order_id].filled==201
        cancel_order(g.buy)
        assert c.portfolio.positions['600000.XSHG'].closeable_amount==0
        assert order_target('600000.XSHG',0) is None
def sell(c):
    if g.day==2:
        assert c.portfolio.positions['600000.XSHG'].closeable_amount==201
        order_target('600000.XSHG',0,LimitOrderStyle(9.5))
def after_trading_end(c):
    if g.day==2:
        assert not c.portfolio.positions and not get_open_orders()
        assert all(o.status==OrderStatus.held for o in get_orders().values())
"""
    result, _, root = run_minute(source, doc=minute_session_doc, config=JQ)
    assert [(t["side"], t["amount"]) for t in result["trades"]] == [
        ("BUY", 200),
        ("BUY", 1),
        ("SELL", 200),
        ("SELL", 1),
    ]
    assert [t["commission"] for t in result["trades"]] == [5, 0, 5, 0]
    audit_run(root)


def test_last_day_pending_state_is_declared_without_invented_cancel_clock(
    run_minute, minute_session_doc
):
    source = """from jqdata import *
def initialize(c): run_daily(buy,'14:59')
def buy(c):
    if str(c.current_dt.date())=='2024-03-06': order('600000.XSHG',1000,LimitOrderStyle(2))
def after_trading_end(c):
    if str(c.current_dt.date())=='2024-03-06': assert len(get_open_orders())==1
def on_strategy_end(c): assert len(get_open_orders())==1
"""
    result, events, root = run_minute(source, doc=minute_session_doc, config=JQ)
    assert len(result["orders"]) == 1 and not result["trades"]
    assert result["orders"][0]["status"] == "held"
    assert result["final_account"]["locked_cash"] == 2000
    assert not any(e["kind"] == "order_expiry_boundary" for e in events)
    assert result["execution_policy"]["platform_exact_cancel_time_verified"] is False
    audit_run(root)


@pytest.mark.parametrize("clock", ["11:30", "12:00", "15:00", "15:30"])
def test_scheduled_outside_order_window_is_explicit(run_minute, minute_session_doc, clock):
    source = f"from jqdata import *\ndef initialize(c): run_daily(buy,{clock!r})\ndef buy(c): order('600000.XSHG',100)"
    with pytest.raises(MinuteRunError, match="JQ_ORDER_OUTSIDE_SESSION"):
        run_minute(source, doc=minute_session_doc, config=JQ)
