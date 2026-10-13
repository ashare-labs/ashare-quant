"""Role and knowledge-time gates for the AD observation consumer."""

import copy
import json

import pytest

from ashare_quant.minute.inputs import MinuteInputs


@pytest.mark.parametrize(
    "kind", ["schema", "purpose", "duplicate", "coverage", "early_daily", "early_close", "replay"]
)
def test_invalid_role_or_clock_is_rejected_before_output(tmp_path, minute_session_doc, kind):
    doc = copy.deepcopy(minute_session_doc)
    obs = doc["session_observations"]
    if kind == "schema":
        doc["schema"] = "ad-minute-backtest.v1"
    elif kind == "purpose":
        obs["closing_marks"][0]["purpose"] = "bar"
    elif kind == "duplicate":
        obs["opening_prices"].append(copy.deepcopy(obs["opening_prices"][0]))
    elif kind == "coverage":
        obs["opening_prices"].pop()
    elif kind in {"early_daily", "early_close"}:
        row = obs["daily" if kind == "early_daily" else "closing_marks"][0]
        row["available_at"] = row["date"] + "T09:30:00+08:00"
    else:
        obs["opening_prices"][0]["replay_available_at"] = obs["opening_prices"][0]["available_at"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        MinuteInputs(path)


def test_opening_scalar_never_reveals_the_unfinished_ohlc(run_minute, minute_session_doc):
    source = """from jqdata import *
def initialize(c): run_daily(check,'09:30')
def check(c):
    if str(c.current_dt.date()) != '2024-03-04': return
    q=get_current_data()['600000.XSHG']
    assert q.day_open==q.last_price==10
    try: get_bars('600000.XSHG',1,'1m',fields=['high'],include_now=True)
    except ValueError as e: assert 'INSUFFICIENT_VISIBLE_HISTORY' in str(e)
    else: raise AssertionError('Opening scalar exposed future minute high')
"""
    run_minute(source, doc=minute_session_doc, config={"lifecycle": "jq_minute"})


def test_independent_close_updates_native_valuation_without_matching_an_extra_bar(
    run_minute, minute_session_doc
):
    for row in minute_session_doc["session_observations"]["closing_marks"]:
        if row["security"] == "600000.XSHG":
            row["price"] = "11"
    source = """from jqdata import *
def initialize(c):
    run_daily(buy,'09:31')
    run_daily(pending,'14:59')
def buy(c):
    if str(c.current_dt.date())=='2024-03-04': order('600000.XSHG',100)
def pending(c): order('600000.XSHG',100,LimitOrderStyle(9))
def after_trading_end(c):
    assert c.portfolio.positions['600000.XSHG'].price==11
    assert len(get_open_orders())==1
"""
    result, events, _ = run_minute(
        source, doc=minute_session_doc, config={"lifecycle": "jq_minute", "matching": "jq_current"}
    )
    assert len(result["trades"]) == 1
    assert result["final_account"]["positions"]["600000.XSHG"]["price"] == 11
    bars = [e for e in events if e["kind"] == "clock" and e["phase"] == "bar"]
    assert len(bars) == 720
    assert not any(e["kind"] == "trade" and e["at"][11:16] >= "15:00" for e in events)
