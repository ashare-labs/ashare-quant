"""Real release queries and native orders; constructed benchmark levels only."""

import csv
import io
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from ashare_data.storage.cmes_release import import_cmes
from fastapi.testclient import TestClient

from tests.minute_tasks.evidence import events as read_events
from tests.minute_tasks.evidence import evidence
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import execute
from tests.minute_tasks.test_daily import fixed_release as fixed_release


def reference(tmp_path, days, missing):
    original = tmp_path / "index.zip"
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["时间", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"])
    writer.writerows([day, 100 + i, 100 + i, 100 + i, 100 + i, "", ""]
                     for i, day in enumerate(days) if day not in missing)
    with ZipFile(original, "w") as archive:
        archive.writestr("SH.000300.csv", stream.getvalue())
    root = tmp_path / "benchmark-release"
    receipt = import_cmes(root, daily=[original], daily_profile="daily-index-points", free_reserve_bytes=0)
    pin = dict(root=str(root), release_id=receipt["release_id"], manifest_sha256=receipt["manifest_sha256"])
    return pin


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
