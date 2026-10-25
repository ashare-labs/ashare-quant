"""Existing cash-market callbacks and empty accounts need no reference prices."""


import math

import pytest
from fastapi.testclient import TestClient

from tests.minute_tasks.evidence import events as read_events
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("fixed_release", [{"execution_fields": [
    "high", "low", "close", "volume", "amount", "preclose", "tradestatus", "is_st",
]}], indirect=True)
def test_reference_calendar_zero_position_and_visible_open_need_no_execution_data(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, end_date=body["start_date"], securities=[], source='''from aquant import *
def initialize(c):
    g.calls = []
    run_daily(before, 'before_open', reference_security='000300.XSHG')
    run_daily(opened, 'open', reference_security='510300.XSHG')
    run_daily(after, 'after_close', reference_security='399001.XSHE')
def empty(c):
    p = c.portfolio
    zero = p.positions['000001.XSHE']
    assert zero.security == '000001.XSHE'
    assert zero.total_amount == zero.closeable_amount == zero.value == zero.avg_cost == 0
    assert '000001.XSHE' not in p.positions and not p.positions
    assert p.positions.get('000001.XSHE') is None
    assert p.positions == p.long_positions == {} and c.securities == ()
    zero.total_amount = 999
    assert p.positions['000001.XSHE'].total_amount == 0
    try: p.positions[('000001.XSHE',)]
    except ValueError as e: assert 'AQUANT_POSITION_SECURITY_REQUIRED' in str(e)
    else: raise AssertionError('multiple-security position key accepted')
    try: p.positions['000300.XSHG']
    except Exception as e: assert getattr(e, 'code', None) == 'AQUANT_UNKNOWN_RELEASE_SECURITY'
    else: raise AssertionError('reference calendar silently authorized missing security')
    assert p.available_cash == p.total_value == 100000 and p.positions_value == 0
def before(c):
    assert c.current_dt.strftime('%H:%M') == '09:00'
    empty(c)
    try: get_current_data()['000001.XSHE'].day_open
    except ValueError as e: assert 'AQUANT_DAY_OPEN_NOT_VISIBLE' in str(e)
    else: raise AssertionError('future opening price visible')
    g.calls.append('before')
def opened(c):
    assert c.current_dt.strftime('%H:%M') == '09:30'
    empty(c)
    record(open=get_current_data()['000001.XSHE'].day_open)
    g.calls.append('open')
def after(c):
    assert c.current_dt.strftime('%H:%M') == '15:30'
    empty(c)
    assert g.calls == ['before', 'open']
    g.calls.append('after')
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 0
        assert result["native"]["final_account"]["positions"] == {}
        assert result["native"]["corporate_entitlements"] == {}
        events = read_events(jobs.directory(job["job_id"]))
        assert [e["name"] for e in events if e["kind"] == "callback_start"] == [
            "initialize", "before", "opened", "after",
        ]
        recorded, = [e for e in events if e["kind"] == "record"]
        # The constructed daily source has a distinct opening price; not yesterday's close.
        assert recorded["values"]["open"] == pytest.approx(round(12 + math.sin(20 / 5) * .3, 2) + .01)


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_unknown_reference_calendar_is_not_assumed_to_be_cash_equity(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, end_date=body["start_date"], securities=[], source='''from aquant import *
def initialize(c):
    run_daily(lambda c: None, reference_security='IF1512.CCFX')
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert "AQUANT_REFERENCE_CALENDAR_UNSUPPORTED" in job["error"]["message"]
