"""Native engineering coverage; the separate real AD/JQ comparison is mandatory."""

import pytest

from examples.jq_minute_comparison.audit_execution import audit_run

JQ = dict(
    lifecycle="jq_minute",
    matching="jq_current",
    capacity_policy="jq_daily_initial",
    cash_policy="resize",
    volume_percent=0.0001,
)


def test_daily_initial_caps_odd_lot_partials_budget_and_next_session_expiry(
    run_minute, minute_session_doc
):
    source = """from jqdata import *
def initialize(c):
    g.day=0
    set_option('order_volume_ratio',0.0001)
    run_daily(submit,'09:31')
    run_daily(partial,'09:32')
    run_daily(cancel,'09:33')
    run_daily(last,'14:56')
def before_trading_start(c):
    g.day+=1
    assert not get_orders() and not get_trades() and not get_open_orders()
    assert abs(c.portfolio.locked_cash) < 1e-7
    if g.day == 2:
        p=c.portfolio.positions['600000.XSHG']
        assert p.today_amount == 0 and p.closeable_amount == p.total_amount
def submit(c):
    if g.day != 1: return
    assert order('600000.XSHG',1000).filled == 200
    assert order('600000.XSHG',1000).filled == 200
    assert order('000001.XSHE',1000).filled == 200
    g.cash=c.portfolio.available_cash
    g.a=order('600000.XSHG',1000,LimitOrderStyle(10.5))
    assert g.a.filled == 200 and g.a.status == OrderStatus.filled
    assert len(get_open_orders()) == 1
    assert abs(c.portfolio.available_cash-(g.cash-10500))<1e-7
def partial(c):
    if g.day == 1: assert get_orders(g.a.order_id)[g.a.order_id].filled == 200
def handle_data(c,data):
    if g.day == 1 and c.current_dt.strftime('%H:%M')=='09:32':
        assert get_orders(g.a.order_id)[g.a.order_id].filled == 201
        assert abs(c.portfolio.available_cash-(g.cash-10500))<1e-7
def cancel(c):
    if g.day == 1:
        assert cancel_order(g.a)
        assert abs(c.portfolio.locked_cash)<1e-7
def last(c):
    if g.day == 1: g.last=order('600000.XSHG',1000,LimitOrderStyle(10.5))
def after_trading_end(c):
    if g.day == 1:
        assert len(get_open_orders())==1
        assert get_orders(g.last.order_id)[g.last.order_id].filled==204
        assert c.portfolio.locked_cash>0
    frame=get_price('600000.XSHG', start_date=str(c.current_dt.date()),
                    end_date=str(c.current_dt.date()), fields=['close','volume'],fq=None)
    assert len(frame)==1 and frame.volume.iloc[0]==2400000
def on_strategy_end(c): assert g.day==3
"""
    result, events, root = run_minute(source, doc=minute_session_doc, config=JQ)
    assert [t["amount"] for t in result["trades"]] == [200, 200, 200, 200, 1, 200, 1, 1, 1, 1]
    assert (
        result["native_classes"]["account"] == "ashare_quant.second_batch.reservation.BudgetAccount"
    )
    assert result["execution_policy"]["retrospective_daily_denominator"]
    assert result["execution_policy"]["platform_exact_cancel_time_verified"] is False
    expiry = [e for e in events if e["kind"] == "order_expiry_boundary"]
    assert len(expiry) == 1 and expiry[0]["at"] == "2024-03-05T09:00:00+08:00"
    assert audit_run(root)["both_cash_components_checked"]


def test_daily_volume_is_not_available_to_strategy_at_initial_fill(run_minute, minute_session_doc):
    source = """from jqdata import *
def initialize(c): run_daily(check,'09:31')
def check(c):
    order('600000.XSHG',1000)
    try: get_price('600000.XSHG',start_date=str(c.current_dt.date()),
                   end_date=str(c.current_dt.date()),fields=['volume'],fq=None)
    except ValueError as e: assert 'INSUFFICIENT_VISIBLE_DAILY' in str(e)
    else: raise AssertionError('post-hoc execution denominator leaked into history')
"""
    result, _, _ = run_minute(source, doc=minute_session_doc, config=JQ)
    assert len(result["trades"]) == 3


def test_daily_policy_requires_source_and_explicit_matching_modes(run_minute):
    with pytest.raises(ValueError, match="REQUIRES_JQ_MATCHING_AND_LIFECYCLE"):
        run_minute("def initialize(c):pass", config={"capacity_policy": "jq_daily_initial"})
    with pytest.raises(ValueError, match="REQUIRES_AD_SESSION_OBSERVATIONS"):
        run_minute("def initialize(c):pass", config=JQ)


def test_tight_buy_budget_keeps_price_improvement_and_paid_minimum_fee(
    run_minute, minute_session_doc
):
    source = """from jqdata import *
def initialize(c):
    run_daily(submit,'09:31')
    run_daily(cancel,'09:33')
def submit(c):
    if str(c.current_dt.date()) == '2024-03-04':
        g.a=order('600000.XSHG',1000,LimitOrderStyle(10.5))
        assert g.a.amount==1000 and g.a.filled==200
def cancel(c):
    if str(c.current_dt.date()) == '2024-03-04':
        assert get_orders(g.a.order_id)[g.a.order_id].filled==201
        assert cancel_order(g.a)
"""
    result, _, root = run_minute(
        source, doc=minute_session_doc, config={**JQ, "initial_cash": 10505}
    )
    assert [t["amount"] for t in result["trades"]] == [200, 1]
    assert [t["commission"] for t in result["trades"]] == [5, 0]
    audit_run(root)
