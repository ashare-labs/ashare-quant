"""Shared daily release fixtures; synthetic evidence only."""

import json
import math
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from ashare_data.financials.tables import TABLES
from ashare_data.storage.financial_release import table_set_hash
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection

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
            minute_start, minute_end = omissions.get("minute_range", [18, 23])
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
                """, [n, days[minute_start], days[minute_end]], f"minutes{n}")
                files.append(dict(item, table="bars_1m", series_id=2, instrument_id=n,
                                  rows=(minute_end-minute_start+1)*240, first_date=days[minute_start], last_date=days[minute_end], duplicate_keys=0))
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
