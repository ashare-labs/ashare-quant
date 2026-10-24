"""Actual published fixtures -> single native strategy execution -> HTTP results."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import execute
from tests.minute_tasks.test_daily import fixed_release as fixed_release


def test_catalogue_reports_calendar_envelope_without_claiming_price_coverage(daily_jobs, fixed_release):
    app, _, _ = daily_jobs
    release, days = fixed_release
    with TestClient(app) as client:
        info = client.get('/api/v1/python/catalogue').json()['data_availability']
        assert info['release_id'] == release.release_id
        assert info['calendar_start'] == days[0] and info['calendar_end'] == days[-1]
        assert info['first_execution_session'] == days[1]
        assert info['last_execution_session'] == days[-2]
        assert 'calendar envelope only' in info['coverage_semantics']


def test_execution_reuses_frozen_objects_and_reference_queries(daily_jobs, fixed_release, tmp_path, monkeypatch):
    from ashare_data.product.released_references import ReleasedReferences
    from ashare_data.storage.market_release import MarketRelease

    from ashare_quant.tasks.minute.contracts import PythonCreate
    from ashare_quant.tasks.minute.demand import DemandInputs

    _, _, body = daily_jobs
    release, _ = fixed_release
    constructions, queries = [], []
    original_init, original_query = MarketRelease.__init__, ReleasedReferences.query

    def opened(self, *args, **kwargs):
        constructions.append(args)
        original_init(self, *args, **kwargs)

    def queried(self, table, condition, parameters):
        queries.append((table, condition, tuple(parameters)))
        return original_query(self, table, condition, parameters)

    monkeypatch.setattr(MarketRelease, "__init__", opened)
    monkeypatch.setattr(ReleasedReferences, "query", queried)
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path)
    try:
        data.component("600000.XSHG", data.days[0])
        first_queries = list(queries)
        for day in data.days[1:6]:
            data.component("600000.XSHG", day)
        assert len(constructions) == 1
        assert queries == first_queries
        assert data.reference_rows > 0
        assert sum(r["rows"] for r in data.reads if r["api"] == "execution_references") == data.reference_rows
        # Another security has its own requested facts, never the first security's result.
        data.component("000001.XSHE", data.days[0])
        assert len(queries) > len(first_queries)
    finally:
        data.close()


@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_minute_open_schedule_uses_existing_clock_without_future_prices(daily_jobs, fixed_release):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.update(frequency="1m", start_date=days[20], end_date=days[22], source="""from aquant import *
def initialize(context):
    set_universe(['600000.XSHG'])
    run_daily(trade, time='open')
def trade(context):
    assert context.current_dt.strftime('%H:%M') == '09:30'
    history = attribute_history('600000.XSHG', 10, '1d', ['close'], fq=None)
    assert history.index[-1].date() == context.previous_date
    aligned = globals()['history'](10, '1d', security_list=['600000.XSHG'], fq=None)
    assert aligned.iloc[-1, 0] == history.close.iloc[-1]
    assert len(get_bars('600000.XSHG', 10, '1d', ['date','close'], df=True)) == 10
    minutes = attribute_history('600000.XSHG', 3, '1m', ['close'], skip_paused=False, fq=None)
    assert minutes.index[-1].date() == context.previous_date
    order_target('600000.XSHG', 100)
""")
    body.pop("securities")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 1
        assert result["native"]["trades"][0]["at"][11:16] == "09:31"
        config = json.loads((jobs.directory(job["job_id"]) / "native-config.json").read_text())
        assert config  # Matching policy is also frozen in the native result assumptions.


@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_daily_callbacks_can_request_visible_minutes(daily_jobs):
    app, jobs, body = daily_jobs
    body.update(end_date=body["start_date"], source="""from aquant import *
def initialize(context):
    set_universe(['600000.XSHG'])
def handle_data(context, data):
    values = attribute_history('000001.XSHE', 10, '1m', ['close'], fq=None)
    assert values.index[-1].date() == context.previous_date
    aligned = history(10, '1m', security_list=['000001.XSHE'], fq=None)
    assert aligned.iloc[-1,0] == values.close.iloc[-1]
    bars = get_bars('000001.XSHE',10,'1m',['date','close'],df=True)
    assert len(bars) == 10 and bars.close.iloc[-1] == values.close.iloc[-1]
    prices = get_price('000001.XSHE',frequency='minute',count=10,fields=['close'],fq=None)
    assert prices.close.iloc[-1] == values.close.iloc[-1]
""")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")


@pytest.mark.parametrize(
    "fixed_release", [{"securities": ["600000.XSHG", "000001.XSHE", "600036.XSHG"]}], indirect=True
)
def test_strategy_catalogue_dynamic_subscription_outside_reads_and_sixty_bars(
    daily_jobs, fixed_release
):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        start_date=days[65],
        end_date=days[68],
        source="""from aquant import *
def configure(context):
    assert context.securities == ()
    g.initializations = 1
    g.calls = 0
    available = get_all_securities(['stock'], date=context.previous_date)
    assert '600036.XSHG' in available.index
    assert available.attrs['historical_pit_verified'] is False
    set_universe(['600036.XSHG'])
initialize = configure
def handle_data(context, data):
    assert g.initializations == 1
    # Read prices outside the subscription, without registering a trade.
    values = attribute_history('000001.XSHE', 60, '1d', ['close'], fq=None)
    assert len(values) == 60
    assert values.index[-1].date() == context.previous_date
    full = get_price('000001.XSHE', start_date='2024-01-15', end_date=context.previous_date,
                     fields=['close'], fq=None)
    assert str(full.index[0].date()) == '2024-01-15'
    if g.calls == 1:
        set_universe(['600000.XSHG'])
        assert context.securities == ('600000.XSHG',)
    if g.calls == 2:
        set_universe([])
    for sid in context.securities:
        order_target(sid, 100)
    g.calls += 1
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        assert client.post("/api/v1/python/jobs", json=body).json()["job_id"] == job["job_id"]
        result = client.get(job["result"]["href"]).json()
        assert result["native"]["real_strategy_executed"]
        assert set(result["native"]["final_account"]["positions"]) == {"600000.XSHG", "600036.XSHG"}
        artifact = json.loads((jobs.directory(job["job_id"]) / "input.json").read_text())
        assert "000001.XSHE" not in artifact["securities"]  # Query != native instruments.
        assert {q["count"] for q in artifact["queries"] if q["api"] == "daily_history"} >= {60}
        assert result["native"]["data_input_sha256"] == result["preparation"]["input_sha256"]
        assert client.get("/api/v1/history/jobs/" + job["job_id"] + "/result").json() == result


@pytest.mark.parametrize(
    "source,error",
    [
        (
            "get_price('600000.XSHG', end_date='2099-01-01', count=1, fq=None)",
            "AQUANT_FUTURE_DATA_REQUEST",
        ),
        (
            "get_price('600000.XSHG', start_date='1990-01-01', fields=['close'], fq=None)",
            "PYTHON_CALENDAR_GAP",
        ),
        (
            "attribute_history('600000.XSHG', 1000, fields=['close'], fq=None)",
            "AQUANT_INSUFFICIENT_VISIBLE_HISTORY",
        ),
    ],
)
@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_demand_refuses_future_unknown_calendar_and_insufficient_history(daily_jobs, source, error, frequency):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        frequency=frequency,
        end_date=body["start_date"],
        source="def initialize(context): pass\ndef handle_data(context,data):\n    "
        + source
        + "\n",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert error in job["error"]["message"], job["error"]
        assert client.get("/api/v1/python/jobs/" + job["job_id"] + "/result").status_code == 409


def test_no_query_cash_only_strategy_needs_no_execution_evidence(daily_jobs):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(source="def initialize(context): pass", end_date=body["start_date"])
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["preparation"]["daily_rows"] == 0
        assert result["summary"]["final_equity"] == body["initial_cash"]


@pytest.mark.parametrize(
    "fixed_release",
    [
        {
            "warmup_fields": [
                "open",
                "high",
                "low",
                "volume",
                "amount",
                "preclose",
                "is_st",
                "tradestatus",
            ],
            "execution_fields": ["preclose", "is_st", "tradestatus"],
        }
    ],
    indirect=True,
)
def test_close_query_needs_no_ohlcv_state_or_execution_references(daily_jobs):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        end_date=body["start_date"],
        source="""from aquant import *
def initialize(context): pass
def handle_data(context,data):
    assert len(attribute_history('000001.XSHE',10,fields=['close'],skip_paused=False,fq=None)) == 10
    assert context.securities == ()
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["preparation"]["securities"] == []
        frozen = json.loads((jobs.directory(job["job_id"]) / "input.json").read_text())
        assert frozen["corporate_schedules"] == {}
        assert all(r["open"] is None for r in frozen["daily"])


@pytest.mark.parametrize(
    "fixed_release",
    [{"calendar_days": 400, "securities": [f"60000{i}.XSHG" for i in range(9)]}],
    indirect=True,
)
def test_nine_subscriptions_and_more_than_126_sessions_without_preloading(
    daily_jobs, fixed_release
):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        start_date=days[1],
        end_date=days[201],
        source="""from aquant import *
def initialize(context):
    set_universe(list(get_all_securities(['stock'],date=context.previous_date).index))
    assert len(context.securities) == 9
def handle_data(context,data):
    if context.current_dt.month > 2:
        set_universe([])
        assert context.securities == ()
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert len(result["native"]["accounts"]) == 201
        assert result["preparation"]["daily_rows"] == 0


@pytest.mark.parametrize(
    "fixed_release",
    [{"minutes": True, "securities": ["600000.XSHG", "000001.XSHE", "600036.XSHG"]}],
    indirect=True,
)
def test_minute_runtime_history_cross_frequency_dynamic_native_subscription(
    daily_jobs, fixed_release
):
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        frequency="1m",
        start_date=days[20],
        end_date=days[21],
        source="""from aquant import *
def initialize(context):
    set_universe(['600036.XSHG'])
    g.day = None
def handle_data(context,data):
    if g.day != context.current_dt.date():
        minute = attribute_history('000001.XSHE',60,'1m',['close'],fq=None)
        assert len(minute) == 60
        assert minute.index[-1] <= context.current_dt
        daily = get_price('000001.XSHE',count=5,fields=['close'],frequency='daily',fq=None)
        assert len(daily) == 5
        if g.day is not None:
            set_universe(['600000.XSHG'])
        order_target(context.securities[0],100)
        g.day = context.current_dt.date()
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 2
        assert set(result["native"]["final_account"]["positions"]) == {"600036.XSHG", "600000.XSHG"}
        artifact = json.loads((jobs.directory(job["job_id"]) / "input.json").read_text())
        assert "000001.XSHE" not in artifact["securities"]


@pytest.mark.parametrize(
    "daily_jobs,call,resource",
    [
        (
            {"query_rows": 3},
            "history(2,'1d','close',['600000.XSHG','000001.XSHE'],fq=None)",
            "query_rows",
        ),
        (
            {"cached_rows": 3},
            "attribute_history('600000.XSHG',4,fields=['close'],fq=None)",
            "cached_rows",
        ),
        (
            {"read_rows": 3},
            "attribute_history('600000.XSHG',4,fields=['close'],fq=None)",
            "read_rows",
        ),
    ],
    indirect=["daily_jobs"],
)
def test_configured_data_budgets_are_reported_without_partial_results(daily_jobs, call, resource):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.update(
        end_date=body["start_date"],
        source="def initialize(context): pass\ndef handle_data(context,data):\n    " + call + "\n",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert "PYTHON_DATA_BUDGET" in job["error"]["message"], job["error"]
        assert (
            resource in job["error"]["message"]
            or job["error"].get("details", {}).get("resource") == resource
        )
        assert client.get("/api/v1/python/jobs/" + job["job_id"] + "/result").status_code == 409


@pytest.mark.parametrize(
    "fixed_release", [{"warmup_fields": ["open", "high", "low", "volume", "amount"]}], indirect=True
)
def test_trade_previous_close_does_not_require_unread_previous_fields(daily_jobs):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.update(
        end_date=body["start_date"],
        source="""from aquant import *
def initialize(context): pass
def handle_data(context,data):
    order_target('600000.XSHG',100)
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        assert client.get(job["result"]["href"]).json()["summary"]["trade_count"] == 1


@pytest.mark.parametrize(
    "fixed_release",
    [
        {
            "action": {
                "announcement_date": "2024-03-01",
                "record_date": "2024-03-25",
                "ex_date": "2024-03-26",
                "payment_date": "2024-03-28",
            }
        }
    ],
    indirect=True,
)
def test_unsubscribed_held_security_still_records_and_receives_distributions(daily_jobs):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.update(
        start_date="2024-03-22",
        end_date="2024-03-28",
        source="""from aquant import *
def initialize(context):
    g.entered = False
def handle_data(context,data):
    if not g.entered:
        order_target('600000.XSHG',100)
        set_universe([])
        g.entered = True
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 1
        trade = result["native"]["trades"][0]
        assert result["native"]["final_account"]["available_cash"] == pytest.approx(
            100000 - 100 * trade["price"] - trade["commission"] + 10
        )
        entries = list(result["native"]["corporate_entitlements"]["600000.XSHG"].values())
        assert len(entries) == 1 and entries[0]["basis"] == 100
        assert "2024-03-28" in entries[0]["stages"].values()


@pytest.mark.parametrize(
    "fixed_release",
    [{"minutes": True, "execution_fields": ["tradestatus", "is_st", "preclose"]}],
    indirect=True,
)
def test_pool_outside_minute_last_price_does_not_prepare_execution(daily_jobs):
    app, jobs, body = daily_jobs
    body.pop("securities")
    body.update(
        frequency="1m",
        end_date=body["start_date"],
        source="""from aquant import *
def initialize(context):
    g.read = False
def handle_data(context,data):
    if not g.read and context.current_dt.strftime('%H:%M') >= '09:31':
        last = get_current_data()['000001.XSHE'].last_price
        prices = attribute_history('000001.XSHE',1,'1m',['close'],fq=None)
        assert last == prices.close.iloc[-1]
        g.read = True
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        root = jobs.directory(job["job_id"])
        document = json.loads((root / "input.json").read_text())
        assert document["corporate_schedules"] == {}
        assert not (root / "execution-cache").exists()
