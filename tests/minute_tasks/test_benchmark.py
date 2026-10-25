"""Real release queries and native orders; constructed benchmark levels only."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.minute_tasks.evidence import events as read_events
from tests.minute_tasks.evidence import evidence
from tests.support.benchmark_release import reference as reference
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("gap", [None, "base", "middle", "unknown"])
@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_benchmark_and_trading_are_independent_frozen_results(daily_jobs, fixed_release, tmp_path, frequency, gap):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    dates = days[19:23]
    missing = [dates[0]] if gap == "base" else [dates[2]] if gap == "middle" else []
    pin = reference(tmp_path, dates, missing)
    selected = "000001.XSHG" if gap == "unknown" else "000300.XSHG"
    profile = {"benchmark_release": {"asset": "indices", "release_id": pin["release_id"],
                                      "manifest_sha256": pin["manifest_sha256"]}}
    jobs.product.configured = lambda: SimpleNamespace(
        document={"profiles": {"product": profile}}, profile="product",
        asset=lambda name: SimpleNamespace(path=pin["root"]))
    body.update(frequency=frequency, securities=[], start_date=dates[1], end_date=dates[-1], source=f'''from aquant import *
def initialize(c):
    set_benchmark('{selected}')
    assert c.securities == () and c.portfolio.positions == {{}}
    run_daily(trade, 'open')
def trade(c):
    order_target('600000.XSHG', 100)
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        benchmark = result["native"]["benchmark"]
        assert benchmark["security"] == selected
        assert len(result["native"]["accounts"]) == 3
        assert result["summary"]["trade_count"] == 1
        assert set(result["native"]["final_account"]["positions"]) == {"600000.XSHG"}
        root = jobs.directory(job["job_id"])
        frozen = evidence(root)
        assert frozen["benchmark_release"] == pin
        assert selected not in frozen["securities"]
        assert json.loads((root / "session.json").read_text())["benchmark_release"] == pin
        if gap is None:
            assert benchmark["status"] == "available"
            assert benchmark["asset_class"] == "index" and benchmark["price_unit"] == "index_point"
            assert benchmark["base"] == {"date": dates[0], "close": 100}
            assert benchmark["prices"] == [{"date": d, "close": 101 + i} for i, d in enumerate(dates[1:])]
            assert benchmark["prices"][-1]["close"] / benchmark["base"]["close"] - 1 == pytest.approx(.03)
            assert any(r["api"] == "benchmark_prices" for r in frozen["reads"])
        else:
            assert benchmark["status"] == "unavailable" and benchmark["base"] is None and benchmark["prices"] == []
            assert benchmark["reason"] == {"base": "BENCHMARK_BASE_MISSING", "middle": "BENCHMARK_DAILY_GAP",
                                           "unknown": "BENCHMARK_SECURITY_UNAVAILABLE"}[gap]
            assert benchmark["missing_dates"] == (dates if gap == "unknown" else missing)
        events = read_events(root)
        assert sum(e.get("kind") == "callback_start" and e.get("name") == "initialize" for e in events) == 1
        assert client.post("/api/v1/python/jobs", json=body).json()["job_id"] == job["job_id"]
        assert client.get('/api/v1/history/jobs/' + job['job_id'] + '/result').json() == result


@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_benchmark_future_boundary_and_index_is_not_a_stock_subscription(daily_jobs, fixed_release, tmp_path):
    from ashare_quant.backtests.minute.inputs import stamp
    from ashare_quant.contracts import DomainError
    from ashare_quant.tasks.minute.benchmark import observations
    from ashare_quant.tasks.minute.contracts import PythonCreate
    from ashare_quant.tasks.minute.demand import DemandInputs
    _, _, body = daily_jobs
    release, days = fixed_release
    body.update(end_date=days[22], securities=[])
    pin = reference(tmp_path, days[19:23], [])
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path, benchmark=pin)
    try:
        with pytest.raises(DomainError, match="future"):
            observations(data, '000300.XSHG', days[20:23], stamp(days[22]+'T14:59:00+08:00'))
        assert data.read_count == 0 and data.ids == []
        # A typed index in a queried publication does not acquire native equity permissions.
        from ashare_data.product.market_release import ReleasedDailyProduct
        data.owner = ReleasedDailyProduct({'market_release': pin})
        with pytest.raises(DomainError, match="equities only"):
            data.register(['000300.XSHG'])
        assert data.ids == []
    finally:
        data.close()


def test_benchmark_selection_is_initialize_only(daily_jobs):
    app, jobs, body = daily_jobs
    body.update(securities=[], end_date=body['start_date'], source='''from aquant import *
def initialize(c):
    run_daily(lambda c: set_benchmark('000300.XSHG'), 'open')
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job['status'] == 'failed'
        assert 'AQUANT_BENCHMARK_ONLY_IN_INITIALIZE' in job['error']['message']


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("selection", ["default", "none"])
@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_new_default_and_explicit_none_preserve_trading(daily_jobs, fixed_release, tmp_path, frequency, selection):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    pin = reference(tmp_path, days[19:23], [])
    profile = {"benchmark_release": {"asset": "indices", "release_id": pin["release_id"],
                                      "manifest_sha256": pin["manifest_sha256"]}}
    jobs.product.configured = lambda: SimpleNamespace(
        document={"profiles": {"product": profile}}, profile="product",
        asset=lambda name: SimpleNamespace(path=pin["root"]))
    selection_code = "set_benchmark(None)" if selection == "none" else "pass"
    body.update(frequency=frequency, securities=[], start_date=days[20], end_date=days[22], source=f'''from aquant import *
def initialize(c):
    {selection_code}
    run_daily(trade, 'open')
def trade(c):
    order_target('600000.XSHG', 100)
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 1
        assert set(result["native"]["final_account"]["positions"]) == {"600000.XSHG"}
        benchmark = result["native"]["benchmark"]
        if selection == "none":
            assert benchmark["status"] == "not_selected"
            assert benchmark["security"] is None and benchmark["selection_source"] == "strategy"
            assert benchmark["prices"] == []
        else:
            assert benchmark["status"] == "available"
            assert benchmark["security"] == "000300.XSHG" and benchmark["selection_source"] == "default"
            assert benchmark["base"] == {"date": days[19], "close": 100}
            assert len(benchmark["prices"]) == 3
