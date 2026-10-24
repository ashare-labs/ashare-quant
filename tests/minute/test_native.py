import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ashare_quant.backtests.minute import run_backtest
from ashare_quant.backtests.minute.runner import MinuteRunError

EXAMPLES = Path(__file__).resolve().parents[2] / "tests/fixtures/research/minute"
ZERO_COST = {"commission_rate": 0, "sell_tax": 0, "min_commission": 0}


def test_first_native_identity_read_preserves_caller_decimal_context():
    import ashare_quant

    env = dict(os.environ, PYTHONPATH=str(Path(ashare_quant.__file__).parents[1]))
    code = """
from decimal import Decimal, getcontext, ROUND_DOWN
from ashare_quant.execution.rqalpha.runtime import runtime_identity
ctx = getcontext()
ctx.prec = 35
ctx.rounding = ROUND_DOWN
expected = Decimal(1) / Decimal(7)
before = str(ctx)
runtime_identity()
assert str(getcontext()) == before, (before, str(getcontext()))
assert Decimal(1) / Decimal(7) == expected
"""
    subprocess.run([sys.executable, "-B", "-c", code], env=env, check=True)


def test_equal_decimal_moving_averages_do_not_create_a_trade(run_minute):
    source = """from decimal import Decimal
from aquant import order, run_daily
def initialize(context):
    run_daily(decide, "09:31")
def decide(context):
    prices = [Decimal("1700.000001")] * 20
    if sum(prices[-5:]) / 5 > sum(prices) / 20:
        order("600000.XSHG", 100)
"""
    result, _, _ = run_minute(source)
    assert result["trades"] == []
    assert result["final_account"]["available_cash"] == 100000


def test_full_sessions_real_python_lifecycle_and_native_ledger(run_minute):
    source = """from aquant import *
def initialize(context):
    g.count = 0
    context.days = 0
    run_daily(opening, 'open')
    run_daily(each_minute, 'every_bar')
    run_daily(closing, 'after_close')
def before_trading_start(context):
    context.days += 1
    assert context.previous_date < context.current_dt.date()
def opening(context):
    assert get_current_data()['600000.XSHG'].last_price > 0
    if context.days == 1:
        order('600000.XSHG', 100)
def each_minute(context):
    g.count += 1
    record(sequence=g.count)
def handle_data(context, data):
    assert data['600000.XSHG'].datetime <= context.current_dt
    assert all(bar.close > 0 for bar in data.values())
def closing(context):
    assert not get_open_orders()
def after_trading_end(context):
    assert g.count == 240 * context.days
"""
    result, events, root = run_minute(source)
    assert result["real_strategy_executed"] and not result["is_test_double"]
    assert not result["real_market_data"] and result["jq_comparison"] == "NOT_PERFORMED"
    assert result["native_classes"]["account"] == "rqalpha.portfolio.account.Account"
    assert result["native_classes"]["broker"].endswith("SimulationBroker")
    callbacks = [e for e in events if e["kind"] == "callback_start"]
    assert sum(e["name"] == "handle_data" for e in callbacks) == 720
    assert not any("11:30" < e["at"][11:16] < "13:00" for e in callbacks)
    assert [e["name"] for e in callbacks[:4]] == [
        "initialize",
        "before_trading_start",
        "opening",
        "each_minute",
    ]
    assert result["trades"][0]["at"] == "2024-03-04T09:31:00+08:00"
    assert result["trades"][0]["price"] == 10 and result["trades"][0]["commission"] == 5
    assert result["final_account"]["available_cash"] == 98995
    assert result["accounts"][0]["positions"]["600000.XSHG"]["closeable_amount"] == 0
    assert result["accounts"][1]["positions"]["600000.XSHG"]["closeable_amount"] == 100
    assert json.loads((root / "teardown.json").read_text())["native_singleton_cleared"]


def test_three_representative_scripts_have_distinct_native_behaviour(run_minute):
    results = {}
    for name in ("equal_weight", "rotation", "limit_and_exit"):
        results[name], _, _ = run_minute((EXAMPLES / (name + ".py")).read_text(), name=name)
    assert {t["security"] for t in results["equal_weight"]["trades"]} == {
        "600000.XSHG",
        "000001.XSHE",
        "000002.XSHE",
    }
    assert {t["security"] for t in results["rotation"]["trades"]} == {"600000.XSHG"}
    limit = results["limit_and_exit"]
    assert limit["trades"][0]["at"] == "2024-03-04T13:01:00+08:00"
    assert [t["amount"] for t in limit["trades"][:3]] == [2500, 2500, 1000]
    assert all(t["at"][:10] > "2024-03-04" for t in limit["trades"] if t["side"] == "SELL")
    assert len({r["final_account"]["total_value"] for r in results.values()}) == 3
    # Independent conservation check against the trade stream, including native fees.
    for r in results.values():
        cash = 100000 - sum(
            (1 if t["side"] == "BUY" else -1) * t["amount"] * t["price"]
            + t["commission"]
            + t["tax"]
            for t in r["trades"]
        )
        assert r["final_account"]["available_cash"] == pytest.approx(cash, abs=1e-8)
        assert r["final_account"]["locked_cash"] == pytest.approx(0, abs=1e-8)
        assert r["final_account"]["total_value"] == pytest.approx(
            cash
            + sum(p["total_amount"] * p["price"] for p in r["final_account"]["positions"].values()),
            abs=1e-8,
        )


def test_shared_cash_partial_limit_cancel_day_expiry_and_t1(run_minute):
    source = """from aquant import *
def initialize(context):
    g.day = 0
    run_daily(opening, 'open')
    run_daily(first_fill, '09:31')
    run_daily(cancel_partial, '09:32')
def before_trading_start(context): g.day += 1
def opening(context):
    if g.day == 1:
        g.a = order('600000.XSHG', 500, LimitOrderStyle(11))
        assert g.a.filled == 0
        assert context.portfolio.locked_cash == 5500
        assert order('000001.XSHE', 300, LimitOrderStyle(21)) is None
    elif g.day == 2:
        assert context.portfolio.positions['600000.XSHG'].closeable_amount == 200
        order_target('600000.XSHG', 0)
def first_fill(context):
    if g.day == 1:
        assert get_orders(g.a.order_id)[g.a.order_id].filled == 100
        assert order('600000.XSHG', -100) is None
        order('000001.XSHE', 100)
def cancel_partial(context):
    if g.day == 1:
        assert get_orders(g.a.order_id)[g.a.order_id].filled == 200
        assert cancel_order(g.a)
        assert not cancel_order(g.a)
        order('600000.XSHG', 100, LimitOrderStyle(1))
def after_trading_end(context):
    assert not get_open_orders()
    assert context.portfolio.locked_cash == 0
"""
    r, events, _ = run_minute(
        source, config={**ZERO_COST, "initial_cash": 10000, "volume_percent": 0.01}
    )
    assert [
        t["amount"] for t in r["trades"] if t["security"] == "600000.XSHG" and t["side"] == "BUY"
    ] == [100, 100]
    assert any(
        o["amount"] == 500 and o["filled"] == 200 and o["status"] == "canceled" for o in r["orders"]
    )
    assert any(o["price"] == 1 and o["status"] == "rejected" for o in r["orders"])
    assert any(e["kind"] == "order_rejection" for e in events)
    # Native market remainder cancels after one capped fill, unlike the day limit.
    assert any(
        o["amount"] == 200 and not o["is_buy"] and o["filled"] == 100 and o["status"] == "canceled"
        for o in r["orders"]
    )


def test_history_current_bars_get_price_and_strict_future_request(run_minute):
    source = """from aquant import *
def initialize(context): run_daily(check, '09:35')
def check(context):
    a = attribute_history('600000.XSHG', 3, '1m', ['close'], fq=None)
    b = get_bars('600000.XSHG', 1, '1m', ['date','close'], include_now=False)
    c = get_price(['600000.XSHG','000001.XSHE'], frequency='1m', count=3, fq=None, fields=['close'])
    assert len(a) == 3 and len(c) == 6
    assert a.index[-1].minute == 35 and b['date'][0].astype('datetime64[m]').astype(str).endswith('09:34')
    assert get_current_data()['600000.XSHG'].last_price == a.close.iloc[-1]
    record(last=float(a.close.iloc[-1]))
"""
    _, events, _ = run_minute(source)
    assert len([e for e in events if e["kind"] == "record"]) == 3


def test_future_prices_do_not_change_prefix_and_late_bars_stay_hidden(run_minute, minute_doc):
    source = """from aquant import *
def initialize(context):
    run_daily(buy, 'open')
    run_daily(check, '09:32')
def buy(context): order('600000.XSHG', 100)
def check(context):
    h = history(1, '1m', 'close', ['600000.XSHG'], fq=None)
    record(last=float(h.iloc[-1,0]), cash=context.portfolio.available_cash)
    if context.current_dt.date().isoformat() == '2024-03-04':
        try:
            get_current_data()['600000.XSHG'].day_open
        except ValueError as exc:
            assert 'MINUTE_DAY_OPEN_NOT_YET_VISIBLE' in str(exc)
        else:
            raise AssertionError('A later bar cannot substitute for the unavailable opening bar')
"""
    doc = copy.deepcopy(minute_doc)
    row = next(r for r in doc["minute"] if r["security"] == "600000.XSHG")
    row["available_at"] = "2024-03-04T09:34:00+08:00"
    r, before, _ = run_minute(source, doc=doc, name="a")
    assert r["trades"][0]["at"].endswith("09:32:00+08:00")
    changed = copy.deepcopy(doc)
    for row in changed["minute"]:
        if row["bar_end"] > "2024-03-04T10:00:00+08:00":
            row.update(open="50", high="51", low="49", close="50")
    _, after, _ = run_minute(source, doc=changed, name="b")
    cutoff = "2024-03-04T10:00:00+08:00"
    assert [e for e in before if e["at"] <= cutoff] == [e for e in after if e["at"] <= cutoff]


@pytest.mark.parametrize(
    "body,error",
    [
        ("run_daily(lambda c: None, '12:00')", "AQUANT_SCHEDULE_TIME_UNSUPPORTED"),
        ("get_fundamentals(None)", "AQUANT_API_UNSUPPORTED:get_fundamentals"),
        ("set_option('avoid_future_data', False)", "AQUANT_OPTION_UNSUPPORTED"),
        ("set_order_cost(OrderCost(close_tax=0))", "AQUANT_COST_MUST_MATCH"),
        ("attribute_history('600000.XSHG', 1)", "AQUANT_ADJUSTMENT_UNSUPPORTED"),
    ],
)
def test_unsupported_requests_are_failures_not_silent_success(run_minute, body, error):
    with pytest.raises(MinuteRunError, match=error):
        run_minute("from aquant import *\ndef initialize(context):\n    " + body + "\n")


def test_completed_reentry_and_failure_preservation(run_minute, tmp_path):
    _, _, root = run_minute((EXAMPLES / "rotation.py").read_text())
    again = run_backtest(tmp_path / "run.py", tmp_path / "run.json", root)
    assert not again["native_run"]
    (root / "events.jsonl").write_text("tampered")
    with pytest.raises(ValueError, match="MINUTE_COMPLETED_ARTIFACT_MISMATCH"):
        run_backtest(tmp_path / "run.py", tmp_path / "run.json", root)
    with pytest.raises(MinuteRunError, match="intentional"):
        run_minute("def initialize(context): raise ValueError('intentional')", name="failed")
    assert not (tmp_path / "failed/complete.json").exists()
    with pytest.raises(ValueError, match="MINUTE_INCOMPLETE_ATTEMPT"):
        run_backtest(tmp_path / "failed.py", tmp_path / "failed.json", tmp_path / "failed")


def test_timeout_retains_failure_and_never_completes(run_minute, tmp_path):
    with pytest.raises(MinuteRunError, match="MINUTE_STRATEGY_TIMEOUT"):
        run_minute("def initialize(context):\n    while True: pass\n", timeout=3)
    assert json.loads((tmp_path / "run/failure.json").read_text())["type"] == "Timeout"
    assert not (tmp_path / "run/complete.json").exists()


def test_suspension_zero_volume_limits_and_shared_minute_capacity(run_minute, minute_doc):
    # Independent input controls exercise the actual matcher and risk validators.
    for state in minute_doc["states"]:
        if state["security"] == "000001.XSHE" and state["date"] == "2024-03-04":
            state["suspended"] = True
    minute_doc["minute"] = [
        r
        for r in minute_doc["minute"]
        if not (r["security"] == "000001.XSHE" and r["bar_end"].startswith("2024-03-04"))
    ]
    for row in minute_doc["minute"]:
        if row["security"] != "600000.XSHG":
            continue
        if row["bar_end"] == "2024-03-04T09:31:00+08:00":
            row.update(volume="0", amount="0")
        if row["bar_end"] == "2024-03-04T09:32:00+08:00":
            row.update(open="100", high="100", low="100", close="100")
    source = """from aquant import *
def initialize(context):
    g.day = 0
    run_daily(opening, 'open')
    run_daily(after_blocked, '09:32')
    run_daily(replace, '09:33')
def before_trading_start(context): g.day += 1
def opening(context):
    if g.day == 1:
        assert get_current_data()['000001.XSHE'].paused
        assert order('000001.XSHE', 100) is None
        g.market = order('600000.XSHG', 100)
        g.limit = order('600000.XSHG', 300, LimitOrderStyle(11))
def after_blocked(context):
    if g.day == 1:
        assert not get_trades()
        assert get_orders(g.market.order_id)[g.market.order_id].status == OrderStatus.canceled
        assert get_orders(g.limit.order_id)[g.limit.order_id].status == OrderStatus.held
def replace(context):
    if g.day == 1:
        assert get_orders(g.limit.order_id)[g.limit.order_id].filled == 100
        cancel_order(g.limit)
        order('600000.XSHG', 100, LimitOrderStyle(11))
        order('600000.XSHG', 100, LimitOrderStyle(11))
"""
    result, _, _ = run_minute(source, config={**ZERO_COST, "volume_percent": 0.01})
    assert [(t["at"][11:16], t["amount"]) for t in result["trades"]] == [
        ("09:33", 100),
        ("09:34", 100),
        ("09:35", 100),
    ]


@pytest.mark.parametrize(
    "body,error",
    [
        (
            "get_price('600000.XSHG', count=1, frequency='1m', fq=None, end_date=context.current_dt.replace(hour=10))",
            "AQUANT_FUTURE_DATA_REQUEST",
        ),
        ("attribute_history('600000.XSHG', 5, '1m', fq=None)", "AQUANT_INSUFFICIENT_VISIBLE_HISTORY"),
        ("order('600000.XSHG', 100)", "AQUANT_ORDER_OUTSIDE_SESSION"),
    ],
)
def test_time_and_history_failures_cannot_be_reported_success(run_minute, body, error):
    with pytest.raises(MinuteRunError, match=error):
        run_minute(
            "from aquant import *\ndef initialize(context): pass\n"
            "def before_trading_start(context):\n    " + body + "\n"
        )


def test_exception_after_a_fill_retains_trace_and_network_tripwire(run_minute, tmp_path):
    source = """from aquant import *
def initialize(context):
    run_daily(buy, 'open')
    run_daily(fail, '09:32')
def buy(context): order('600000.XSHG', 100)
def fail(context): raise RuntimeError('failure-after-native-fill')
"""
    with pytest.raises(MinuteRunError, match="failure-after-native-fill"):
        run_minute(source)
    events = [json.loads(line) for line in (tmp_path / "run/events.jsonl").read_text().splitlines()]
    assert sum(e["kind"] == "trade" for e in events) == 1
    assert not (tmp_path / "run/complete.json").exists()
    with pytest.raises(MinuteRunError, match="EXTERNAL_NETWORK_DISABLED"):
        run_minute(
            "import socket\ndef initialize(context): socket.getaddrinfo('example.com', 443)\n",
            name="network",
        )


def test_original_crlf_script_bytes_are_frozen_and_executed(run_minute, tmp_path):
    _, _, root = run_minute(
        "from aquant import *\r\ndef initialize(context):\r\n    g.value = 1\r\n"
    )
    assert (root / "strategy.py").read_bytes() == (tmp_path / "run.py").read_bytes()
