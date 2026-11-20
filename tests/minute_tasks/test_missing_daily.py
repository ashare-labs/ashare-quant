"""Missing observations stay visible while account valuation uses only past prices."""

from copy import deepcopy

import pytest
from ashare_data.product.market_release import ReleasedDailyProduct
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from ashare_quant.tasks.minute.prepare import listing_facts, prepare_market
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release

SID = "600000.XSHG"


def request(days, **updates):
    return PythonCreate.model_validate({
        "name": "Missing observations", "idempotency_key": "missing-observations",
        "source": "pass", "frequency": "1d", "securities": [SID],
        "start_date": days[20], "end_date": days[23], "warmup_sessions": 1,
        **updates,
    })


def test_batch_never_backfills_from_future_or_mutates_price_response(fixed_release):
    release, days = fixed_release
    owner = ReleasedDailyProduct({"market_release": release.receipt()})
    daily = owner.prices([SID], days[20], days[23])  # Previous session is absent.
    original = deepcopy(daily)
    with pytest.raises(DomainError, match="Daily observations are missing"):
        prepare_market(release.receipt(), request(days), owner=owner, daily=daily)
    assert daily == original


def test_batch_carry_is_untradable_and_labelled_as_an_assumption(fixed_release):
    release, days = fixed_release
    owner = ReleasedDailyProduct({"market_release": release.receipt()})
    daily = owner.prices([SID], days[19], days[23])
    previous = next(r for r in daily["rows"] if r["date"] == days[20])
    daily["rows"] = [r for r in daily["rows"] if r["date"] not in days[21:23]]
    original = deepcopy(daily)
    metadata, prepared, _ = prepare_market(release.receipt(), request(days), owner=owner, daily=daily)
    assert daily == original
    for day in days[21:23]:
        row = next(r for r in prepared["rows"] if r["date"] == day)
        state = next(r for r in metadata["states"] if r["date"] == day)
        assert row["close"] == previous["close"] and row["volume"] == "0"
        assert row["source"] == "research_carry_forward"
        assert row["assumption"]["reference_date"] == days[20]
        assert state["suspended"] is True
        assert state["source_observation"]["suspended"]["status"] == "assumed"
    assert metadata["states"][-1]["source_observation"]["previous_close"]["status"] == "assumed"
    assert len(metadata["daily_assumptions"]) == 2


@pytest.mark.parametrize("fixed_release", [{"missing_listings": [SID]}], indirect=True)
def test_price_coverage_is_not_an_invented_ipo(fixed_release):
    release, days = fixed_release
    fact = listing_facts(release, [SID])[SID]
    assert fact["ipo"] is None and fact["status"] == "unknown"
    assert fact["first_observed_date"] == days[0]
    owner = ReleasedDailyProduct({"market_release": release.receipt()})
    metadata, _, _ = prepare_market(release.receipt(), request(days), owner=owner)
    assert metadata["states"][0]["first_listing_date"] is None
    assert metadata["states"][0]["source_observation"]["first_listing_date"]["status"] == "unknown"


@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("missing_previous", [False, True])
def test_consecutive_missing_days_and_return_to_observed_prices(
    fixed_release, tmp_path, monkeypatch, frequency, missing_previous,
):
    release, days = fixed_release
    data = DemandInputs(release.receipt(), request(days, frequency=frequency), tmp_path / "job")
    original_prices = data.owner.prices
    missing = days[19 if missing_previous else 20:22]

    def prices(*args, **kwargs):
        response = original_prices(*args, **kwargs)
        response["rows"] = [r for r in response["rows"] if r["date"] not in missing]
        return response

    monkeypatch.setattr(data.owner, "prices", prices)
    try:
        for day in days[20:23]:
            state = data.component(SID, day).state(SID, day)
            assert state["suspended"] is (day in missing)
        assert data.preflight()["assumed_daily_rows"] == len(missing)
        assert data._daily(SID, [days[20]]) == [None]  # Raw data is still missing.
        # A later component/cache must never leak its price into an earlier request.
        earlier = data.component(SID, days[20]).state(SID, days[20])
        expected = original_prices([SID], days[18], days[18])["rows"][0] if missing_previous else original_prices([SID], days[19], days[19])["rows"][0]
        assert earlier["previous_close"] == expected["close"]
    finally:
        data.close()


@pytest.mark.parametrize("fixed_release", [{"minutes": True, "missing_days": [20, 21]}], indirect=True)
@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_native_account_crosses_missing_days_without_trading_them(daily_jobs, fixed_release, frequency):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.update(frequency=frequency, start_date=days[19], end_date=days[22],
                warmup_sessions=1, securities=[SID])
    body["source"] = f'''from aquant import *
def initialize(context):
    set_benchmark(None)
    run_daily(rebalance, "{'open' if frequency == '1d' else '09:50'}")
    run_daily(inspect_quote, "before_open")
def inspect_quote(context):
    assert get_current_data()[context.securities[0]].last_price > 0
def rebalance(context):
    sid = context.securities[0]
    quote = get_current_data()[sid]
    assert quote.last_price > 0
    assert quote.paused == (str(context.current_dt.date()) in {days[20:22]!r})
    assert quote.is_st is False
    order_target(sid, 100 if str(context.current_dt.date()) == "{days[19]}" else 0)
'''
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        trades = result["native"]["trades"]
        assert len(trades) == 2
        assert [t["at"][:10] for t in trades] == [days[19], days[22]]
        assert result["preparation"]["assumed_daily_rows"] == 2
        assert {r["date"] for r in result["preparation"]["daily_assumptions"]} == set(days[20:22])
