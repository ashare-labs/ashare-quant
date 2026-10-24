"""Constructed fixed release -> real owner queries -> native daily Python -> HTTP result.

This is execution evidence, not real-market or CMES acceptance.
"""

import json
import math
import sqlite3
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from ashare_data.financials.tables import TABLES
from ashare_data.storage.financial_release import table_set_hash
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.tasks.worker import work_one
from tests.support.python_sources import DAILY_SOURCE

IDS = ["600000.XSHG", "000001.XSHE"]


@pytest.fixture
def fixed_release(tmp_path, request):
    omissions = getattr(request, "param", {})
    ids = omissions.get("securities", IDS)

    def observed(value, field, index):
        absent = (index < 20 and field in omissions.get("warmup_fields", ())) or (
            index == 20 and field in omissions.get("execution_fields", ())
        )
        return None if absent else value

    days = [
        (date(2024, 1, 1) + timedelta(days=i)).isoformat()
        for i in range(omissions.get("calendar_days", 130))
        if (date(2024, 1, 1) + timedelta(days=i)).weekday() < 5
    ]
    root = tmp_path / "constructed-market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        store = LocalDailyStore(root / "work")
        db = store.db
        db.executescript("""
            CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
            CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
            CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT,release_id INTEGER,
                parser_sha256 TEXT,mapping_sha256 TEXT,body TEXT);
        """)
        db.execute(
            "INSERT INTO data_batches VALUES(1,'fixture',1,'fixture','fixture',?)",
            [json.dumps({"manifest": {"target_start": days[0], "target_end": days[-1]}})],
        )
        db.executemany("INSERT INTO calendar VALUES(?,'constructed',NULL)", [(d,) for d in days])
        for n, sid in enumerate(ids, 1):
            symbol = ("sh" if sid.endswith("XSHG") else "sz") + sid[:6]
            db.execute("INSERT INTO release_instruments VALUES(?,?,?)", [n, sid, "fixture"])
            db.execute("INSERT INTO release_identifiers VALUES(?,?)", [sid, n])
            db.execute(
                "INSERT INTO listing_facts VALUES(?,?,'2000-01-01',NULL,'{}','2025-01-01',NULL)",
                [sid, symbol],
            )
        db.commit()
        store.close()
        c = connection(memory="128MB", threads=1)
        c.execute("""CREATE TABLE prices(series_id INTEGER,instrument_id INTEGER,trade_date DATE,
            open_price DECIMAL(18,6),high_price DECIMAL(18,6),low_price DECIMAL(18,6),
            close_price DECIMAL(18,6),volume_shares BIGINT,turnover_cny DOUBLE,batch_id INTEGER,
            source_symbol VARCHAR,source VARCHAR,row_hash VARCHAR,response_id VARCHAR,
            observed_at VARCHAR,preclose VARCHAR,tradestatus VARCHAR,is_st VARCHAR)""")
        files = []
        for n, sid in enumerate(ids, 1):
            prices = [round(10 + n + math.sin(i / 5) * 0.3, 2) for i in range(len(days))]
            rows = [
                (
                    1,
                    n,
                    d,
                    observed(prices[i] + 0.01, "open", i),
                    observed(prices[i] + 0.02, "high", i),
                    observed(prices[i] - 0.02, "low", i),
                    observed(prices[i], "close", i),
                    observed(100000, "volume", i),
                    observed(100000 * prices[i], "amount", i),
                    1,
                    ("sh" if sid.endswith("XSHG") else "sz") + sid[:6],
                    "constructed",
                    f"{sid}:{d}",
                    None,
                    "2025-01-01T00:00:00+00:00",
                    observed(str(prices[max(0, i - 1)]), "preclose", i),
                    observed("1", "tradestatus", i),
                    observed("0", "is_st", i),
                )
                for i, d in enumerate(days[:-1])
            ]
            c.executemany("INSERT INTO prices VALUES(" + ",".join("?" for _ in rows[0]) + ")", rows)
            item = writer.parquet(c, "SELECT * FROM prices WHERE instrument_id=?", [n], f"bars{n}")
            files.append(
                dict(
                    item,
                    table="bars_1d",
                    series_id=1,
                    instrument_id=n,
                    rows=len(rows),
                    first_date=days[0],
                    last_date=days[-2],
                    duplicate_keys=0,
                )
            )
        if omissions.get("minutes"):
            for n, sid in enumerate(ids, 1):
                item = writer.parquet(c, """
                    SELECT 2::INTEGER series_id,instrument_id,trade_date,
                      (trade_date::VARCHAR || 'T00:00:00+08:00')::TIMESTAMPTZ +
                        m.minute * INTERVAL '1 minute' AS label_at,
                      open_price,open_price AS high_price,open_price AS low_price,
                      open_price AS close_price,10000::BIGINT volume_shares,
                      10000 * open_price AS turnover_cny,1::INTEGER batch_id
                    FROM prices CROSS JOIN
                      (SELECT range AS minute FROM range(571,691)
                       UNION ALL SELECT range FROM range(781,901)) m
                    WHERE instrument_id=? AND trade_date BETWEEN ? AND ?
                """, [n, days[18], days[23]], f"minutes{n}")
                files.append(dict(item, table="bars_1m", series_id=2, instrument_id=n,
                                  rows=6*240, first_date=days[18], last_date=days[23], duplicate_keys=0))
        evidence = []
        coverage, captures, actions = [], [], []
        for sid in ids:
            for source in ("sina_actions", "sina_structure"):
                key = sid + source
                coverage.append(
                    dict(
                        coverage_id=key,
                        batch_key="fixture",
                        task_id=key,
                        source=source,
                        security=sid,
                        source_state="complete",
                        input_rows=1,
                        mapped_rows=1,
                        quarantined_rows=0,
                        coverage_json=json.dumps(
                            dict(
                                source_status="parsed_provider_tables",
                                counts={"actions": 1},
                                table_states={
                                    name: dict(
                                        state="provider_no_rows_reported",
                                        explicit_empty_marker=True,
                                    )
                                    for name in ("sharebonus_1", "sharebonus_2")
                                },
                            )
                        ),
                    )
                )
                captures.append(
                    dict(
                        capture_id=key,
                        batch_key="fixture",
                        task_id=key,
                        source=source,
                        observed_at="2025-01-01T00:00:00+00:00",
                        source_state="complete",
                    )
                )
            actions.append(
                dict(
                    instrument_id=ids.index(sid) + 1,
                    security=sid,
                    event_id=sid + "-past-action",
                    event_type="distribution",
                    implemented=True,
                    source="sina_actions",
                    source_revision_id="constructed",
                    source_fields_json=json.dumps(
                        dict(
                            fields={
                                "announcement_date": "2023-05-01",
                                "record_date": "2023-05-09",
                                "ex_date": "2023-05-10",
                                "payment_date": "2023-05-10",
                                "cash_per_share": "0.1",
                                "bonus_share_ratio": "0",
                                "capitalization_share_ratio": "0",
                                **omissions.get("action", {}),
                            }
                        )
                    ),
                )
            )
        for index, (table, rows) in enumerate(
            {
                "meta.financial_coverage": coverage,
                "meta.financial_captures": captures,
                "corporate_action.events": actions,
            }.items()
        ):
            schema, name = TABLES[table], f"refs{index}"
            c.execute(
                "CREATE TABLE " + name + "(" + ",".join(f"{k} {v}" for k, v in schema.items()) + ")"
            )
            c.executemany(
                "INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in schema) + ")",
                [[r.get(k) for k in schema] for r in rows],
            )
            item = writer.parquet(c, "SELECT * FROM " + name, [], name)
            evidence.append(
                dict(
                    item,
                    kind="financial_table",
                    table=table,
                    rows=len(rows),
                    partition_key="fixture",
                    key_value_mismatches=0,
                )
            )
        c.close()
        contract = dict(
            schema="ad.financial-tables.v1",
            table_names=list(TABLES),
            candidate_ids=[],
            table_set_sha256=table_set_hash(evidence),
        )
        with sqlite3.connect(root / "work/daily.sqlite3") as db:
            db.execute("CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT)")
            db.execute(
                "INSERT INTO release_contracts VALUES('financial_reference_tables',?)",
                [json.dumps(contract)],
            )
        db.close()
        meta = writer.object(root / "work/daily.sqlite3", "sqlite3")
        writer.publish(
            dict(
                metadata=meta,
                files=files,
                evidence=evidence,
                financial_reference_tables=contract,
                financial_table_validation={"table_set_sha256": contract["table_set_sha256"]},
                input_dispositions={"constructed": {"input_rows": 1, "adopted": 1}},
            ),
            expected_parent=None,
        )
    return MarketRelease(root), days


@pytest.fixture
def daily_jobs(tmp_path, fixed_release, monkeypatch, request):
    if hasattr(request, "param"):
        monkeypatch.setenv("ASHARE_QUANT_JOB_POLICY", json.dumps(request.param))
    release, days = fixed_release
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    jobs = app.state.python_jobs
    # Only the deployment selector is replaced. Preparation queries actual frozen AD objects.
    jobs.product = SimpleNamespace(call=lambda f: f(SimpleNamespace(release=release)),
        configured=lambda: SimpleNamespace(document={"profiles": {"product": {}}}, profile="product"))
    monkeypatch.setattr(jobs, "environment", lambda: "a" * 64)
    body = dict(
        environment_id="a" * 64,
        name="Imported daily MA",
        idempotency_key="daily-one",
        source=DAILY_SOURCE,
        parameters={"lookback": 5, "shares": 100},
        frequency="1d",
        warmup_sessions=20,
        securities=IDS,
        start_date=days[20],
        end_date=days[80],
        initial_cash=100000,
        accept_research_model=True,
        trusted_local_code=True,
    )
    return app, jobs, body


def execute(client, jobs, body):
    response = client.post("/api/v1/python/jobs", json=body)
    assert response.status_code == 201, response.text
    job = response.json()
    assert work_one(jobs, job["job_id"]) == job["job_id"]
    current = client.get("/api/v1/python/jobs/" + job["job_id"]).json()
    return current


def test_daily_python_preparation_native_equity_parameters_and_reload(daily_jobs, record_property):
    app, jobs, body = daily_jobs
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["native"]["real_strategy_executed"]
        assert result["native"]["schema"] == "aq-python-daily-result.v1"
        assert result["preparation"]["daily_rows"] == 132  # Actual 5-bar lookback plus execution days.
        assert len(result["native"]["accounts"]) == 61
        assert result["summary"]["trade_count"] > 4
        assert result["summary"]["final_equity"] != 100000
        assert result["logs"]
        assert all(
            t["at"][11:16] == "09:30" and t["amount"] == 100 for t in result["native"]["trades"]
        )
        frozen = json.loads((jobs.directory(job["job_id"]) / "input.json").read_text())
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
        events = [
            json.loads(line)
            for line in (jobs.directory(job["job_id"]) / "events.jsonl").read_text().splitlines()
        ]
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
    from ashare_data.providers.cmes_factors import import_cmes_factors

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
        events = [
            json.loads(line)
            for line in (jobs.directory(job_id) / "events.jsonl").read_text().splitlines()
        ]
        reads = [r for r in events if r["kind"] == "daily_history_read"]
        assert {r["price_basis"] for r in reads} == {"raw", "pre", "post"}
        assert all(r["receipt_sha256"] for r in reads if r["price_basis"] != "raw")
