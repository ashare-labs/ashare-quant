"""Engineering checks of the explicitly selected, real-reference JQ clock model."""

import pytest

from ashare_quant.backtests.minute.lifecycle import handle_clocks


def test_complete_callback_order_lunch_and_terminal_without_fabricated_bars(run_minute):
    source = """from aquant import *
def initialize(c):
    assert c.current_dt.strftime('%H:%M') == '00:00'
    assert c.run_params.type == 'full_backtest'
    g.days=0; g.clocks=[]
    for clock in ['09:30','09:31','11:29','11:30','12:00','13:00','13:01','14:59','15:00']:
        run_daily(boundary,clock)
    run_daily(each,'every_bar')
def before_trading_start(c):
    g.days+=1; g.clocks=[]; g.each=0
def boundary(c):
    record(count=len(g.clocks), hour=c.current_dt.hour, minute=c.current_dt.minute)
def each(c): g.each+=1
def handle_data(c,data): g.clocks.append(c.current_dt.strftime('%H:%M'))
def after_trading_end(c):
    assert len(g.clocks) == g.each == 240
    assert '11:30' not in g.clocks and '15:00' not in g.clocks
def on_strategy_end(c):
    assert str(c.current_dt).endswith('23:59:59.001000')
    assert g.days == 3
    record(complete=g.days)
"""
    _, events, _ = run_minute(source, config={"lifecycle": "jq_minute"})
    calls = [e for e in events if e["kind"] == "callback_start"]
    for day in ("2024-03-04", "2024-03-05", "2024-03-06"):
        actual = [e["at"] for e in calls if e["name"] == "handle_data" and e["at"][:10] == day]
        assert actual == [t.isoformat() for t in handle_clocks(day)]
    assert [e["name"] for e in calls[:5]] == [
        "initialize",
        "before_trading_start",
        "boundary",
        "each",
        "handle_data",
    ]
    records = [e["values"] for e in events if e["kind"] == "record"]
    assert [r["count"] for r in records[:9]] == [0, 1, 119, 120, 120, 120, 121, 239, 240]
    assert calls[-1]["name"] == "on_strategy_end"
    assert records[-1] == {"complete": 3}
    # Lunch clocks have no native BAR, even when a user callback runs there.
    assert not any(
        e["kind"] == "clock" and e["phase"] == "bar" and "11:30" < e["at"][11:16] < "13:00"
        for e in events
    )


def test_full_lifecycle_does_not_make_first_unfinished_minute_visible(run_minute):
    source = """from aquant import *
def initialize(c): run_daily(opening,'09:30')
def opening(c):
    if str(c.current_dt.date()) != '2024-03-04': return
    try: attribute_history('600000.XSHG',1,'1m',['high'],fq=None)
    except ValueError as e: assert 'INSUFFICIENT_VISIBLE_HISTORY' in str(e)
    else: raise AssertionError('future first-minute OHLC leaked')
"""
    run_minute(source, config={"lifecycle": "jq_minute"})


@pytest.mark.parametrize("key,value", [("lifecycle", "jq"), ("capacity_policy", None)])
def test_policy_options_are_strict_before_running(run_minute, key, value):
    with pytest.raises(ValueError, match="MINUTE_CONFIG_POLICY"):
        run_minute("def initialize(c): pass", config={key: value})


def test_terminal_day_queries_end_without_erasing_native_ledger(run_minute):
    source = """from aquant import *
def initialize(c): run_daily(buy,'09:31')
def buy(c):
    if str(c.current_dt.date())=='2024-03-06': g.order=order('600000.XSHG',100)
def after_trading_end(c):
    if str(c.current_dt.date())=='2024-03-06':
        assert len(get_orders())==len(get_trades())==1
def on_strategy_end(c):
    assert get_orders()==get_orders(g.order.order_id)==get_trades()=={}
    assert c.portfolio.positions['600000.XSHG'].total_amount==100
"""
    result, _, _ = run_minute(source, config={"lifecycle": "jq_minute"})
    assert len(result["orders"]) == len(result["trades"]) == 1
    assert result["final_account"]["positions"]["600000.XSHG"]["total_amount"] == 100
    assert result["execution_policy"]["terminal_day_queries"] == "empty"
