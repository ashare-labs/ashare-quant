"""Native execution invariants, independent of the two reference strategies."""

import json
from collections import defaultdict

import pytest

from ashare_quant.backtests.minute import run_backtest
from ashare_quant.backtests.minute.runner import MinuteRunError
from tests.fixtures.minute_reference.audit_execution import audit_run

ZERO = {"commission_rate": 0, "sell_tax": 0, "min_commission": 0}


def flat(doc, *, volume="10000"):
    for row in doc["minute"]:
        price = str(10 + doc["securities"].index(row["security"]) * 10)
        row.update(
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            amount=str(int(price) * int(volume)),
        )
    return doc


def one_row(doc, clock, sid="600000.XSHG", day="2024-03-04"):
    return next(
        r
        for r in doc["minute"]
        if r["security"] == sid and r["bar_end"] == f"{day}T{clock}:00+08:00"
    )


@pytest.mark.parametrize("matching", ["next_open", "jq_current"])
@pytest.mark.parametrize("volume", [99499, 99500, 99999, 100000, 199500, 199999, 200000])
def test_fractional_capacity_cannot_be_promoted_to_a_lot(run_minute, minute_doc, matching, volume):
    flat(minute_doc)
    one_row(minute_doc, "09:31").update(volume=str(volume), amount=str(volume * 10))
    one_row(minute_doc, "09:32").update(volume="100000", amount="1000000")
    clock = "open" if matching == "next_open" else "09:31"
    source = f"""from aquant import *
def initialize(c):
    g.day = 0
    run_daily(buy, {clock!r})
def before_trading_start(c): g.day += 1
def buy(c):
    if g.day == 1: order('600000.XSHG', 300, LimitOrderStyle(10.5))
"""
    result, events, _ = run_minute(
        source, config={**ZERO, "volume_percent": 0.001, "matching": matching}
    )
    fills = defaultdict(int)
    for trade in result["trades"]:
        fills[trade["at"][11:16]] += trade["amount"]
    assert fills["09:31"] == (volume // 100000) * 100
    assert fills["09:32"] == 100
    assert all(o["status"] in {"rejected", "filled"} for o in result["orders"])
    if volume in (99500, 99999, 199500, 199999):
        assert any(
            e["kind"] == "volume_capacity" and e["strict_remaining"] < e["native_proposal"]
            for e in events
        )


@pytest.mark.parametrize("matching", ["next_open", "jq_current"])
def test_exact_capacity_is_shared_by_orders_but_not_by_stocks(run_minute, minute_doc, matching):
    flat(minute_doc, volume="299500")
    clock = "open" if matching == "next_open" else "09:31"
    source = f"""from aquant import *
def initialize(c):
    g.day=0
    run_daily(buy, {clock!r})
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day != 1: return
    order('600000.XSHG', 200)
    order('600000.XSHG', 200)
    order('000001.XSHE', 100)
"""
    result, _, _ = run_minute(
        source, config={**ZERO, "volume_percent": 0.001, "matching": matching}
    )
    assert [(t["security"], t["amount"]) for t in result["trades"]] == [
        ("600000.XSHG", 200),
        ("000001.XSHE", 100),
    ]
    assert result["orders"][1]["status"] == "canceled"


@pytest.mark.parametrize(
    "matching,clock,price", [("next_open", "09:32", 11), ("jq_current", "09:31", 10)]
)
def test_explicit_price_clock_and_synchronous_return(
    run_minute, minute_doc, matching, clock, price
):
    flat(minute_doc)
    one_row(minute_doc, "09:32").update(open="11", high="12", low="9", close="10.5")
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(buy, '09:31')
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day == 1:
        assert get_current_data()['600000.XSHG'].last_price == 10
        o=order('600000.XSHG',100)
        record(filled_before_return=o.filled)
"""
    result, events, _ = run_minute(source, config={**ZERO, "matching": matching})
    assert [(t["at"][11:16], t["price"]) for t in result["trades"]] == [(clock, price)]
    records = [e["values"] for e in events if e["kind"] == "record"]
    assert records == [{"filled_before_return": 100 if matching == "jq_current" else 0}]


@pytest.mark.parametrize(
    "api,value",
    [
        ("order", 1000),
        ("order_target", 1000),
        ("order_value", 10000),
        ("order_target_value", 10000),
    ],
)
@pytest.mark.parametrize("cash,expected", [(1004.99, 0), (1005, 100), (2505, 200)])
def test_native_cash_resize_respects_fee_and_round_lot_for_all_order_apis(
    run_minute, minute_doc, api, value, cash, expected
):
    flat(minute_doc)
    source = f"""from aquant import *
def initialize(c):
    g.day=0
    run_daily(buy, '09:31')
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day == 1: {api}('600000.XSHG', {value})
"""
    result, _, root = run_minute(
        source,
        config={
            "initial_cash": cash,
            "matching": "jq_current",
            "cash_policy": "resize",
            "sell_tax": 0.0005,
        },
    )
    assert sum(t["amount"] for t in result["trades"]) == expected
    assert result["final_account"]["available_cash"] == pytest.approx(
        cash - expected * 10 - (5 if expected else 0)
    )
    native = json.loads((root / "native-config.json").read_text())
    assert native["mod"]["sys_risk"]["validate_cash"]
    assert native["mod"]["sys_accounts"]["auto_switch_order_value"]


def test_resize_keeps_shared_cash_and_t1_in_native_account(run_minute, minute_doc):
    flat(minute_doc)
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(trade, '09:31')
def before_trading_start(c): g.day+=1
def trade(c):
    if g.day == 1:
        assert order('600000.XSHG',100).filled == 100
        assert order('000001.XSHE',1000).amount == 100
        assert order('600000.XSHG',1000) is None
        assert order('600000.XSHG',-100) is None
        assert c.portfolio.positions['600000.XSHG'].closeable_amount == 0
    elif g.day == 2:
        assert c.portfolio.positions['600000.XSHG'].closeable_amount == 100
        order_target('600000.XSHG',0)
        order_target('000001.XSHE',0)
"""
    result, _, _ = run_minute(
        source,
        config={
            "initial_cash": 3500,
            "matching": "jq_current",
            "cash_policy": "resize",
            "sell_tax": 0.0005,
        },
    )
    assert [t["amount"] for t in result["trades"]] == [100] * 4
    assert result["final_account"]["available_cash"] == pytest.approx(3478.5)
    assert not result["final_account"]["positions"]


def test_resting_limits_strict_cross_price_lunch_cancellation_and_day_expiry(
    run_minute, minute_doc
):
    flat(minute_doc)
    for clock, low in (("09:32", "9.5"), ("09:33", "9.49"), ("13:01", "9.5"), ("13:02", "9.49")):
        one_row(minute_doc, clock).update(low=low)
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(first,'09:31')
    run_daily(lunch,'11:30')
    run_daily(resume,'13:00')
    run_daily(cancel,'13:03')
    run_daily(expire,'14:58')
def before_trading_start(c): g.day+=1
def first(c):
    if g.day == 1:
        g.a=order('600000.XSHG',300,LimitOrderStyle(9.5))
        assert g.a.filled == 0
def lunch(c):
    if g.day == 1:
        assert get_orders(g.a.order_id)[g.a.order_id].filled == 100
        assert len(get_open_orders()) == 1
def resume(c):
    if g.day == 1: assert len(get_open_orders()) == 1
def cancel(c):
    if g.day == 1:
        assert get_orders(g.a.order_id)[g.a.order_id].filled == 200
        assert cancel_order(g.a)
def expire(c):
    if g.day == 1: order('600000.XSHG',100,LimitOrderStyle(9))
def after_trading_end(c): assert not get_open_orders()
"""
    result, _, _ = run_minute(
        source, config={**ZERO, "volume_percent": 0.01, "matching": "jq_current"}
    )
    assert [(t["at"][11:16], t["amount"], t["price"]) for t in result["trades"]] == [
        ("09:33", 100, 9.5),
        ("13:02", 100, 9.5),
    ]
    assert [o["status"] for o in result["orders"]] == ["canceled", "rejected"]
    assert result["final_account"]["locked_cash"] == pytest.approx(0, abs=1e-7)


@pytest.mark.parametrize("clock,late", [("open", False), ("13:00", False), ("09:31", True)])
def test_current_price_requires_an_actual_visible_closed_bar(run_minute, minute_doc, clock, late):
    if late:
        one_row(minute_doc, "09:31")["available_at"] = "2024-03-04T09:32:00+08:00"
    source = f"""from aquant import *
def initialize(c): run_daily(buy,{clock!r})
def buy(c): order('600000.XSHG',100)
"""
    with pytest.raises(MinuteRunError, match="AQUANT_CURRENT_REQUIRES_VISIBLE_CLOSED_BAR"):
        run_minute(source, config={"matching": "jq_current"})


@pytest.mark.parametrize(
    "policy",
    [{"matching": "guess"}, {"matching": True}, {"cash_policy": []}, {"cash_policy": "ignore"}],
)
def test_unknown_or_unsafe_execution_policy_is_rejected_before_execution(
    tmp_path, minute_doc, policy
):
    script, inputs = tmp_path / "s.py", tmp_path / "i.json"
    script.write_text("def initialize(c): pass\n")
    inputs.write_text(json.dumps(minute_doc))
    with pytest.raises(ValueError, match="MINUTE_CONFIG_POLICY"):
        run_backtest(script, inputs, tmp_path / "run", config=policy)
    assert not (tmp_path / "run").exists()


def test_policy_change_cannot_reuse_completed_result(tmp_path, minute_doc):
    script, inputs = tmp_path / "s.py", tmp_path / "i.json"
    script.write_text("def initialize(c): pass\n")
    inputs.write_text(json.dumps(minute_doc))
    root = tmp_path / "run"
    run_backtest(script, inputs, root)
    original = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    for policy in ({"matching": "jq_current"}, {"cash_policy": "resize"}):
        with pytest.raises(ValueError, match="MINUTE_COMPLETED_IDENTITY_MISMATCH"):
            run_backtest(script, inputs, root, config=policy)
        assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == original


@pytest.mark.parametrize(
    "commission,minimum,cash,expected",
    [
        (0.02, 7.1, 1019.99, 0),
        (0.02, 7.1, 1020, 100),
        (0.001, 7.1, 1007.09, 0),
        (0.001, 7.1, 1007.1, 100),
    ],
)
def test_resize_uses_configured_costs_not_the_reference_fee(
    run_minute, minute_doc, commission, minimum, cash, expected
):
    flat(minute_doc)
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(buy,'09:31')
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day == 1: order('600000.XSHG',1000)
"""
    result, _, root = run_minute(
        source,
        config={
            "initial_cash": cash,
            "commission_rate": commission,
            "min_commission": minimum,
            "matching": "jq_current",
            "cash_policy": "resize",
        },
    )
    assert sum(t["amount"] for t in result["trades"]) == expected
    audit_run(root)


def test_cash_resize_does_not_disable_next_bar_cash_risk_check(run_minute, minute_doc):
    flat(minute_doc)
    one_row(minute_doc, "09:32").update(open="11", high="11", low="11", close="11")
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(buy,'09:31')
def before_trading_start(c): g.day+=1
def buy(c):
    if g.day == 1: assert order('600000.XSHG',1000).amount == 100
"""
    result, _, _ = run_minute(source, config={"initial_cash": 1005, "cash_policy": "resize"})
    assert result["trades"] == []
    assert result["orders"][0]["status"] == "rejected"
    assert result["final_account"]["available_cash"] == pytest.approx(1005)


def test_partial_order_cumulative_cost_cancel_reorder_and_sell_tax(run_minute, minute_doc):
    flat(minute_doc)
    for row in minute_doc["minute"]:
        row["low"] = str(float(row["close"]) - 0.1)
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(start,'09:31')
    run_daily(replace,'09:32')
def before_trading_start(c): g.day+=1
def start(c):
    if g.day == 1: g.first=order('600000.XSHG',300,LimitOrderStyle(10))
    elif g.day == 2: order_target('600000.XSHG',0,LimitOrderStyle(10))
def replace(c):
    if g.day == 1:
        assert get_orders(g.first.order_id)[g.first.order_id].filled == 200
        assert cancel_order(g.first)
        order('600000.XSHG',100,LimitOrderStyle(10))
"""
    # Resting sales need a strict high cross too.
    for row in minute_doc["minute"]:
        row["high"] = str(float(row["close"]) + 0.1)
    result, _, root = run_minute(
        source,
        config={
            "volume_percent": 0.01,
            "matching": "jq_current",
            "commission_rate": 0.004,
            "min_commission": 7.1,
            "sell_tax": 0.001,
        },
    )
    assert [t["commission"] for t in result["trades"]] == pytest.approx(
        [7.1, 0.9, 7.1, 7.1, 0.9, 4]
    )
    assert [t["tax"] for t in result["trades"]] == pytest.approx([0, 0, 0, 1, 1, 1])
    assert result["final_account"]["available_cash"] == pytest.approx(99969.9)
    assert not result["final_account"]["positions"]
    assert audit_run(root)["capacity_violations"] == []


def test_resting_sell_requires_strict_high_cross_and_fills_at_limit(run_minute, minute_doc):
    flat(minute_doc)
    one_row(minute_doc, "09:32", day="2024-03-05")["high"] = "10.5"
    one_row(minute_doc, "09:33", day="2024-03-05")["high"] = "10.51"
    source = """from aquant import *
def initialize(c):
    g.day=0
    run_daily(trade,'09:31')
def before_trading_start(c): g.day+=1
def trade(c):
    if g.day == 1: order('600000.XSHG',100)
    elif g.day == 2: order('600000.XSHG',-100,LimitOrderStyle(10.5))
"""
    result, _, root = run_minute(source, config={**ZERO, "matching": "jq_current"})
    assert [(t["side"], t["at"][11:16], t["price"]) for t in result["trades"]] == [
        ("BUY", "09:31", 10),
        ("SELL", "09:33", 10.5),
    ]
    audit_run(root)
