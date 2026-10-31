"""Constructed fixed release -> real owner queries -> native daily Python -> HTTP result.

This is execution evidence, not real-market or CMES acceptance.
"""

import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.tasks.worker import work_one
from tests.minute_tasks.evidence import events as read_events
from tests.minute_tasks.evidence import evidence
from tests.support.daily_release import IDS as IDS
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute as execute
from tests.support.daily_release import fixed_release as fixed_release


def test_daily_python_preparation_native_equity_parameters_and_reload(daily_jobs, record_property):
    app, jobs, body = daily_jobs
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["native"]["real_strategy_executed"]
        assert result["native"]["schema"] == "aq-python-daily-result.v1"
        assert result["preparation"]["daily_rows"] >= 132  # Actual reads include cache misses; windows are not retained forever.
        assert len(result["native"]["accounts"]) == 61
        assert result["summary"]["trade_count"] > 4
        assert result["summary"]["final_equity"] != 100000
        assert result["logs"]
        assert all(
            t["at"][11:16] == "09:30" and t["amount"] == 100 for t in result["native"]["trades"]
        )
        frozen = evidence(jobs.directory(job["job_id"]))
        prices = {(r["security"], r["date"]): r for r in frozen["daily"]}
        assert all(
            t["price"] == float(prices[t["security"], t["at"][:10]]["open"])
            for t in result["native"]["trades"]
        )
        final = result["native"]["final_account"]
        assert final["total_value"] == pytest.approx(
            final["available_cash"]
            + sum(
                p["total_amount"] * float(prices[s, body["end_date"]]["close"])
                for s, p in final["positions"].items()
            )
        )
        events = read_events(jobs.directory(job["job_id"]))
        reads = [r for r in events if r["kind"] == "daily_history_read"]
        assert reads and all(r["dates"][-1] < r["at"][:10] for r in reads)
        second = execute(
            client,
            jobs,
            body | {"idempotency_key": "daily-two", "parameters": {"lookback": 5, "shares": 200}},
        )
        assert second["status"] == "succeeded", second.get("error")
        larger = client.get(second["result"]["href"]).json()
        assert larger["summary"]["final_equity"] != result["summary"]["final_equity"]
        assert all(t["amount"] == 200 for t in larger["native"]["trades"])
        record_property(
            "fixed_release_scope", "constructed prices; 2 securities; 61 sessions; 5 requested history bars"
        )
        record_property("shares_100", json.dumps(result["summary"]))
        record_property("shares_200", json.dumps(larger["summary"]))
    with TestClient(
        create_app(str(app.state.store.path), diagnostics_configuration={})
    ) as refreshed:
        assert refreshed.get("/api/v1/python/jobs/" + job["job_id"]).json()["status"] == "succeeded"
        assert refreshed.get(job["result"]["href"]).json() == result
    assert work_one(jobs, job["job_id"]) is None


def test_daily_callback_error_location_and_queued_cancellation(daily_jobs):
    app, jobs, body = daily_jobs
    body["source"] = (
        "def initialize(context): pass\ndef handle_data(context, data):\n    raise ValueError('daily-boom')\n"
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert "第 3 行" in job["error"]["message"]
        assert client.get("/api/v1/python/jobs/" + job["job_id"] + "/result").status_code == 409
        queued = client.post(
            "/api/v1/python/jobs", json=body | {"idempotency_key": "cancel"}
        ).json()
        cancelled = client.post(
            "/api/v1/python/jobs/" + queued["job_id"] + "/cancel", json={}
        ).json()
        assert cancelled["status"] == "cancelled" and not cancelled["can_resume"]


@pytest.mark.parametrize("fixed_release,skip_paused", [
    ({"warmup_fields": ["preclose", "is_st"]}, True),
    ({"warmup_fields": ["preclose", "is_st", "tradestatus"]}, False),
], indirect=["fixed_release"])
def test_daily_warmup_only_requires_fields_used_by_history(daily_jobs, skip_paused):
    app, jobs, body = daily_jobs
    body["source"] = f'''from aquant import *
def initialize(context): pass
def handle_data(context, data):
    for sid in context.securities:
        values = attribute_history(sid, 5, fields=['close'], skip_paused={skip_paused}, fq=None)
        assert len(values) == 5
        order_target(sid, 100)
def after_trading_end(context):
    # A one-day pause-filtered query must not inspect older, unused warmup status.
    assert len(attribute_history(context.securities[0], 1, fq=None)) == 1
'''
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["summary"]["trade_count"] == 2


@pytest.mark.parametrize("fixed_release", [{"warmup_fields": ["tradestatus"]}], indirect=True)
def test_daily_pause_filter_refuses_unknown_warmup_status(daily_jobs):
    app, jobs, body = daily_jobs
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert job["error"]["code"] == "PYTHON_DAILY_HISTORY_STATE_GAP"
        assert job["error"]["details"]["field"] == "tradestatus"
        assert job["error"]["source_line"] is not None


@pytest.mark.parametrize("fixed_release", [
    {"execution_fields": ["tradestatus"]}, {"execution_fields": ["is_st"]},
    {"execution_fields": ["preclose"]},
], indirect=True)
def test_daily_execution_fields_remain_required(daily_jobs):
    app, jobs, body = daily_jobs
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed"
        assert job["error"]["code"] in {"PYTHON_STATE_GAP", "PYTHON_DAILY_VALUE_GAP"}
        assert client.get('/api/v1/python/jobs/' + job["job_id"] + '/result').status_code == 409


def test_custom_daily_history_shapes_close_visibility_and_end_callback(daily_jobs):
    app, jobs, body = daily_jobs
    body["source"] = """from aquant import *
def initialize(context):
    assert context.run_params.frequency == "daily"
    set_universe(context.securities)
def handle_data(context, data):
    sid = context.securities[0]
    assert data[sid].datetime.date() == context.previous_date
    h = history(3, "1d", "close", fq=None)
    assert h.shape == (3, 2) and h.index[-1].date() == context.previous_date
    bars = get_bars(sid, 3, unit="1d", fields=["date", "close"], df=True)
    assert list(bars.close) == list(h[sid])
    grouped = get_price(context.securities, count=3, end_date=context.previous_date, fq=None)
    assert len(grouped) == 6 and set(grouped.code) == set(context.securities)
    ranged = get_price(sid, start_date=str(h.index[0].date()), end_date=context.previous_date, fq=None)
    assert len(ranged) == 3
def after_trading_end(context):
    row = get_price(context.securities[0], count=1, fq=None)
    assert row.index[-1].date() == context.current_dt.date()
def on_strategy_end(context):
    log.info("finished-daily")
"""
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["logs"][-1]["message"] == "finished-daily"


def test_running_daily_python_can_cancel_without_result_or_resume(daily_jobs):
    app, jobs, body = daily_jobs
    body["source"] = "def initialize(context):\n    import time\n    while True: time.sleep(.01)\n"
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        stopped = []

        def cancel(stage, *args):
            phase = jobs.directory(job_id) / "phase.json"
            if (
                stage == "poll"
                and not stopped
                and phase.exists()
                and json.loads(phase.read_text())["phase"] == "executing"
            ):
                stopped.append(client.post("/api/v1/python/jobs/" + job_id + "/cancel", json={}))

        work_one(jobs, job_id, _stage=cancel)
        job = client.get("/api/v1/python/jobs/" + job_id).json()
        assert stopped and stopped[0].status_code == 200
        assert job["status"] == "cancelled" and not job["can_resume"]
        assert job["progress"]["terminal"] and job["progress"]["current_rss_bytes"] is None
        assert job["progress"]["completed_trading_days"] == 0
        assert client.get("/api/v1/python/jobs/" + job_id + "/result").status_code == 409


@pytest.mark.parametrize(
    "statement,reason",
    [
        (
            "get_price(context.securities[0], end_date='2025-01-01', fq=None)",
            "AQUANT_FUTURE_DATA_REQUEST",
        ),
        (
            "attribute_history(context.securities[0], 100, fq=None)",
            "AQUANT_INSUFFICIENT_VISIBLE_HISTORY",
        ),
        ("attribute_history(context.securities[0], 3)", "AQUANT_ADJUSTMENT_UNAVAILABLE"),
    ],
)
def test_daily_history_refuses_future_missing_and_unprovided_adjustment(
    daily_jobs, statement, reason
):
    app, jobs, body = daily_jobs
    body["source"] = (
        "from aquant import *\ndef initialize(context): pass\ndef handle_data(context, data):\n    "
        + statement
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "failed" and reason in job["error"]["message"], job.get("error")


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_daily_adjusted_history_uses_frozen_owner_snapshot(
    daily_jobs, tmp_path, monkeypatch, frequency,
):
    from ashare_data import Store
    from ashare_data.providers.cmes import import_cmes_factors

    from ashare_quant.providers.daily.history import configured_factors

    app, jobs, body = daily_jobs
    paths = []
    for mode, initial, updated in (("hfq", 1, 2), ("qfq", 2, 1)):
        path = tmp_path / (mode + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            for member in ("SH.600000.csv", "SZ.000001.csv"):
                archive.writestr(
                    member, f"date,{mode}_factor\n1900-01-01,{initial}\n2024-01-30,{updated}\n"
                )
        paths.append(path)
    store = Store.init(tmp_path / "factor-source")
    identity = import_cmes_factors(
        store,
        hfq_path=paths[0],
        qfq_path=paths[1],
        members=dict(zip(IDS, ("SH.600000.csv", "SZ.000001.csv"), strict=True)),
        through="2024-12-31",
    )
    binding = tmp_path / "factor-binding.json"
    binding.write_text(json.dumps({"root": str(store.root), "dataset_id": identity}))
    monkeypatch.setenv("ASHARE_QUANT_PYTHON_HISTORY_FACTORS", str(binding))
    jobs.history_factors = configured_factors()
    body.update(frequency=frequency, end_date="2024-02-01")
    body["source"] = """from aquant import *
def initialize(context):
    run_daily(read_daily, time='open')
def read_daily(context):
    sid = context.securities[0]
    raw = attribute_history(sid, 10, fields=['close', 'volume', 'money'], fq=None)
    pre = attribute_history(sid, 10, fields=['close', 'volume', 'money'], fq='pre')
    post = get_price(sid, count=10, fields=['close', 'volume', 'money'], fq='post')
    assert pre['volume'].equals(raw['volume']) and post['money'].equals(raw['money'])
    assert pre.index[-1].date() < context.current_dt.date()
    assert pre.attrs['adjustment']['basis_date'] == str(pre.index[-1].date())
    # The snapshot contains a future event. It must not affect earlier signals.
    if str(pre.index[-1].date()) < '2024-01-30':
        assert pre['close'].equals(raw['close'])
    elif str(pre.index[0].date()) < '2024-01-30':
        assert abs(pre['close'].iloc[0] * 2 - raw['close'].iloc[0]) < 1e-8
    h = history(10, security_list=[sid], fq='pre')
    bars = get_bars(sid, 10, fields=['close'], fq_ref_date=context.previous_date, df=True)
    assert h[sid].equals(pre['close']) and bars['close'].equals(pre['close'])
    order_target(sid, 100)
"""
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        # A submitted task uses its own frozen snapshot even if the selector disappears.
        (store.root / "cmes_factors" / (identity + ".json")).unlink()
        binding.unlink()
        monkeypatch.delenv("ASHARE_QUANT_PYTHON_HISTORY_FACTORS")
        assert work_one(jobs, job_id) == job_id
        job = client.get("/api/v1/python/jobs/" + job_id).json()
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["preparation"]["history_factors"]["dataset_id"] == identity
        events = read_events(jobs.directory(job_id))
        reads = [r for r in events if r["kind"] == "daily_history_read"]
        assert {r["price_basis"] for r in reads} == {"raw", "pre", "post"}
        assert all(r["receipt_sha256"] for r in reads if r["price_basis"] != "raw")
