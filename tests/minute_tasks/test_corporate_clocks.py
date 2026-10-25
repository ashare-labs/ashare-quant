"""The same daily callback settles actions in both native Python clock models."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("fixed_release", [{"minutes": True, "action": {
    "record_date": "2024-01-29", "ex_date": "2024-01-30", "payment_date": "2024-01-31",
}}], indirect=True)
@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("hold", [False, True])
def test_daily_callback_and_dividend_work_with_both_clocks(daily_jobs, frequency, hold):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, start_date="2024-01-29", end_date="2024-02-01", source=f"""from aquant import *
def initialize(c):
    set_universe(['600000.XSHG'])
    run_daily(buy, 'open')
def buy(c):
    assert c.current_dt.strftime('%H:%M') == '09:30'
    prices = attribute_history('600000.XSHG', 10, '1d', ['close'], fq=None)
    assert prices.index[-1].date() == c.previous_date
    assert len(prices) == 10
    if {hold}: order_target('600000.XSHG', 100)
""")
    body.pop("securities")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        native = result["native"]
        assert len(native["trades"]) == int(hold)
        assert len(native["accounts"]) == 4
        expected = body["initial_cash"]
        if hold:
            rights, = native["corporate_entitlements"]["600000.XSHG"].values()
            assert rights["basis"] == 100
            assert Decimal(rights["cash"]) == 10
            assert set(rights["stages"].values()) == {"2024-01-29", "2024-01-30", "2024-01-31"}
            trade, = native["trades"]
            expected += 10 - trade["price"] * trade["amount"] - trade["commission"]
            assert native["final_account"]["positions"]["600000.XSHG"]["dividend_receivable"] == 0
        else:
            assert native["corporate_entitlements"]["600000.XSHG"] == {}
        assert native["final_account"]["available_cash"] == pytest.approx(expected)
        assert not (jobs.directory(job["job_id"]) / "native.chk").exists()
