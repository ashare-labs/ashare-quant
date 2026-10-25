"""Tiny, synthetic immutable AD release. Never real market acceptance evidence.

Uses the installed AD writer and actual AQ worker without replacing business logic.
Only caller-owned temporary directories are written. No network data or credentials.
"""

import json
import sqlite3
from pathlib import Path

SID = "000001.XSHE"
DAYS = ["2024-03-01", "2024-03-04", "2024-03-05", "2024-03-06"]
SOURCE = """from aquant import *
def initialize(context):
    set_universe(context.securities)
    run_daily(trade, time="09:50")
def trade(context):
    target = int(context.parameters['shares']) if str(context.current_dt.date()) == '2024-03-04' else 0
    order_target(context.securities[0], target)
    log.info('synthetic MCP engineering experiment', target)
"""


def make_deployment(root, *, minute_readers=None):
    from ashare_data.financials.tables import TABLES
    from ashare_data.storage.local_daily import LocalDailyStore
    from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection

    from ashare_quant.deployment import resolve
    from ashare_quant.deployment.runtime import initialize

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with ReleaseWriter(root / "market", free_reserve_bytes=0) as w:
        store = LocalDailyStore(w.root / "work" / "daily")
        store.close()
        meta = w.root / "work/daily/daily.sqlite3"
        with sqlite3.connect(meta) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript("""
            CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
            INSERT INTO release_instruments VALUES(1,'000001.XSHE','SYNTHETIC ENGINEERING ONLY');
            CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
            INSERT INTO release_identifiers VALUES('000001.XSHE',1);
            CREATE TABLE release_daily_summary(instrument_id INTEGER,source TEXT,rows INTEGER,first TEXT,last TEXT);
            INSERT INTO release_daily_summary VALUES(1,'synthetic',4,'2024-03-01','2024-03-06');
            CREATE TABLE data_batches(batch_id INTEGER,batch_key TEXT,body TEXT);
            CREATE TABLE source_assets(asset_id TEXT,body TEXT);
            CREATE TABLE source_aliases(source_version TEXT,instrument_id INTEGER);
            CREATE TABLE release_contracts(name TEXT,body TEXT);
            INSERT INTO instruments VALUES('sz000001','SYNTHETIC','1991-04-03',NULL,'main','synthetic','2024-03-07',NULL);
            INSERT INTO listing_facts VALUES('synthetic-listing','sz000001','1991-04-03',NULL,'{}','2024-03-07',NULL);
            """)
            db.executemany("INSERT INTO calendar VALUES(?,'synthetic',NULL)", [(d,) for d in DAYS])
            db.execute(
                "INSERT INTO data_batches VALUES(1,'fixture',?)",
                [json.dumps(dict(manifest=dict(target_start=DAYS[0], target_end=DAYS[-1])))],
            )
            db.execute(
                "INSERT INTO release_contracts VALUES('minute_selection',?)",
                [json.dumps(dict(sources=dict(tdx=3), canonical_series=[3]))],
            )
            if minute_readers is not None:
                db.execute(
                    "INSERT INTO release_contracts VALUES('source_observation_readers',?)",
                    [json.dumps(minute_readers)],
                )
        c = connection(memory="128MB", threads=1)
        c.execute("SET TimeZone = 'UTC'")
        c.execute("CREATE TABLE dates(d DATE, n INTEGER)")
        c.executemany("INSERT INTO dates VALUES(?,?)", [(d, n) for n, d in enumerate(DAYS)])
        daily = w.parquet(
            c,
            """SELECT 1::INTEGER series_id,1::INTEGER instrument_id,d trade_date,
          (10+n*0.1)::DECIMAL(38,12) open_price,(10.2+n*0.1)::DECIMAL(38,12) high_price,
          (9.9+n*0.1)::DECIMAL(38,12) low_price,(10.1+n*0.1)::DECIMAL(38,12) close_price,
          2400000::BIGINT volume_shares,24000000::DOUBLE turnover_cny,1::INTEGER batch_id,
          'sz000001' source_symbol,'synthetic' AS source,'synthetic-' || d::VARCHAR row_hash,
          NULL::VARCHAR response_id,'2024-03-07T00:00:00+00:00' observed_at,
          (10+n*0.1)::VARCHAR preclose,'1' tradestatus,'0' is_st FROM dates""",
            [],
            "daily",
        )
        daily.update(
            table="bars_1d",
            series_id=1,
            instrument_id=1,
            rows=4,
            first_date=DAYS[0],
            last_date=DAYS[-1],
            duplicate_keys=0,
        )
        minute = w.parquet(
            c,
            """SELECT 3::INTEGER series_id,1::INTEGER instrument_id,d trade_date,
          (d::TIMESTAMP + INTERVAL '1 hour 30 minutes' +
            (CASE WHEN m<=120 THEN m ELSE m+90 END) * INTERVAL '1 minute')::TIMESTAMPTZ label_at,
          (10+n*0.1)::DOUBLE open_price,(10.2+n*0.1)::DOUBLE high_price,
          (9.9+n*0.1)::DOUBLE low_price,(10.1+n*0.1)::DOUBLE close_price,
          10000::BIGINT volume_shares,100000::DOUBLE turnover_cny,1::INTEGER batch_id
          FROM dates CROSS JOIN range(1,241) t(m) WHERE n IN (1,2)""",
            [],
            "minute",
        )
        minute.update(
            table="bars_1m",
            series_id=3,
            instrument_id=1,
            rows=480,
            first_date=DAYS[1],
            last_date=DAYS[2],
            duplicate_keys=0,
        )
        evidence = []
        records = {"meta.financial_coverage": [], "meta.financial_captures": []}
        for source in ("sina_actions", "sina_structure"):
            # Synthetic records exercising the owner contract, not provider evidence.
            records["meta.financial_coverage"].append(
                dict(
                    coverage_id="synthetic-" + source,
                    batch_key="fixture",
                    task_id=source,
                    source=source,
                    security=SID,
                    source_state="complete",
                    input_rows=0,
                    mapped_rows=0,
                    quarantined_rows=0,
                    coverage_json=json.dumps(
                        dict(
                            source_status="parsed_provider_tables",
                            counts={},
                            table_states={
                                name: dict(
                                    state="provider_no_rows_reported", explicit_empty_marker=True
                                )
                                for name in ("sharebonus_1", "sharebonus_2")
                            },
                        )
                    ),
                )
            )
            records["meta.financial_captures"].append(
                dict(
                    capture_id="synthetic-" + source,
                    batch_key="fixture",
                    task_id=source,
                    source=source,
                    observed_at="2024-03-07T00:00:00+00:00",
                    source_state="complete",
                )
            )
        records["corporate_action.events"] = [
            dict(
                event_id="synthetic-prior-distribution",
                instrument_id=1,
                security=SID,
                event_type="distribution",
                implemented=True,
                source="sina_actions",
                source_revision_id="synthetic-only",
                record_date="2024-02-27",
                ex_date="2024-02-28",
                payment_date="2024-02-28",
                announcement_date="2024-02-20",
                gross_cash="0.1",
                bonus_ratio="0",
                capitalization_ratio="0",
                quality_reasons_json="[]",
                source_fields_json=json.dumps(
                    dict(
                        fields=dict(
                            announcement_date="2024-02-20",
                            record_date="2024-02-27",
                            ex_date="2024-02-28",
                            payment_date="2024-02-28",
                            cash_per_share="0.1",
                            bonus_share_ratio="0",
                            capitalization_share_ratio="0",
                        )
                    )
                ),
            )
        ]
        for i, (name, rows) in enumerate(records.items()):
            fields = TABLES[name]
            table = "fixture_" + str(i)
            c.execute(
                "CREATE TABLE "
                + table
                + "("
                + ",".join(f"{k} {v}" for k, v in fields.items())
                + ")"
            )
            c.executemany(
                "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in fields) + ")",
                [[row.get(k) for k in fields] for row in rows],
            )
            item = w.parquet(c, "SELECT * FROM " + table, [], table)
            item.update(
                kind="financial_table",
                table=name,
                rows=len(rows),
                partition_key="synthetic",
                key_value_mismatches=0,
            )
            evidence.append(item)
        c.close()
        from ashare_data.storage.financial_release import table_set_hash

        contract = dict(
            schema="ad.financial-tables.v1",
            table_names=list(TABLES),
            candidate_ids=[],
            table_set_sha256=table_set_hash(evidence),
        )
        with sqlite3.connect(meta) as db:
            db.execute(
                "INSERT INTO release_contracts VALUES('financial_reference_tables',?)",
                [json.dumps(contract)],
            )
        w.publish(
            dict(
                metadata=w.object(meta, "sqlite3"),
                files=[daily, minute],
                evidence=evidence,
                financial_reference_tables=contract,
                financial_table_validation=dict(table_set_sha256=contract["table_set_sha256"]),
                provenance="SYNTHETIC MCP ENGINEERING FIXTURE; NOT MARKET OBSERVATIONS",
                input_dispositions={"synthetic": dict(input_rows=484, adopted=484)},
            ),
            expected_parent=None,
        )
    release = MarketRelease(root / "market")
    document = dict(
        schema="ashare-deployment.v1",
        data_root=".",
        assets={
            "daily": dict(kind="local_daily", path="empty-daily", role="market"),
            "market": dict(kind="market_release", path="market", role="market"),
            "releases": dict(kind="store", path="releases", role="publication"),
        },
        profiles={
            "product": dict(
                kind="product",
                daily_asset="daily",
                release_store_asset="releases",
                control_db="state/control.sqlite3",
                paper_root="state/paper",
                market_release=dict(
                    asset="market", release_id=release.release_id, manifest_sha256=release.sha256
                ),
                reference_sets={"formal": dict(kind="market_release")},
                default_reference_set="formal",
                default_minute_source="canonical",
            )
        },
    )
    path = root / "deployment.json"
    path.write_text(json.dumps(document))
    LocalDailyStore(root / "empty-daily").close()
    config = resolve(path)
    initialize(config)
    return config


def submission(environment_id, key="synthetic-100", shares=100, source=SOURCE):
    return dict(
        environment_id=environment_id,
        name="Synthetic MCP engineering experiment",
        idempotency_key=key,
        source=source,
        parameters=dict(shares=shares),
        securities=[SID],
        start_date=DAYS[1],
        end_date=DAYS[2],
        initial_cash=100000,
        accept_research_model=True,
        trusted_local_code=True,
    )
