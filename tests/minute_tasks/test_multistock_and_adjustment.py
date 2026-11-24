"""Multi-security LRU batching, cross-sectional NaN alignment, and release-derived fq."""

import json
import sqlite3

import pytest
from ashare_data.financials.tables import TABLES
from ashare_data.storage.financial_release import table_set_hash
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from fastapi.testclient import TestClient

from ashare_quant.data import ASHARE_COMMIT, ASHARE_TREE, ASHARE_VERSION, ASHARE_WHEEL_SHA256
from ashare_quant.market.reader import ResearchOwner
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from tests.minute_tasks.evidence import evidence
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


def _split_release(tmp_path):
    """Construct a minimal release with a 2-for-1 split on day index 4 and a late-IPO stock."""
    days = [f"2024-03-{d:02d}" for d in (4, 5, 6, 7, 8, 11, 12, 13)]
    ids = ["600000.XSHG", "000001.XSHE", "600036.XSHG"]
    root = tmp_path / "split-market"
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
                "INSERT INTO listing_facts VALUES(?,?,'2020-01-01',NULL,'{}','2025-01-01',NULL)",
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
        # 600000.XSHG: closes at 20.0 on days[0..3]; 2-for-1 split on days[4] -> preclose=10.0, close=10.5
        s1_rows = []
        for i, d in enumerate(days[:-1]):
            if i < 4:
                cl, pre = 20.0, 20.0
            elif i == 4:
                cl, pre = 10.5, 10.0
            else:
                cl, pre = 11.0, 10.5 if i == 5 else 11.0
            s1_rows.append((
                1, 1, d, cl, cl + 0.2, cl - 0.2, cl, 100000, cl * 100000, 1,
                "sh600000", "constructed", f"s1:{d}", None, "2025-01-01T00:00:00+00:00",
                f"{pre:.2f}", "1", "0",
            ))
        # 000001.XSHE: missing observation on days[2]
        s2_rows = [
            (
                1, 2, d, 15.0, 15.2, 14.8, 15.0, 100000, 1500000.0, 1,
                "sz000001", "constructed", f"s2:{d}", None, "2025-01-01T00:00:00+00:00",
                "15.00", "1", "0",
            )
            for i, d in enumerate(days[:-1])
            if i != 2
        ]
        # 600036.XSHG: newly listed starting only on days[4]
        s3_rows = [
            (
                1, 3, d, 30.0, 30.5, 29.5, 30.0, 100000, 3000000.0, 1,
                "sh600036", "constructed", f"s3:{d}", None, "2025-01-01T00:00:00+00:00",
                "30.00", "1", "0",
            )
            for i, d in enumerate(days[:-1])
            if i >= 4
        ]
        for n, group in ((1, s1_rows), (2, s2_rows), (3, s3_rows)):
            c.executemany(
                "INSERT INTO prices VALUES(" + ",".join("?" for _ in group[0]) + ")", group
            )
            first_d, last_d = group[0][2], group[-1][2]
            item = writer.parquet(
                c, "SELECT * FROM prices WHERE instrument_id=?", [n], f"bars{n}"
            )
            files.append(
                dict(
                    item, table="bars_1d", series_id=1, instrument_id=n,
                    rows=len(group), first_date=first_d, last_date=last_d, duplicate_keys=0,
                )
            )
        ev = []
        coverage, captures, actions = [], [], []
        for sid in ids:
            for source in ("sina_actions", "sina_structure"):
                key = sid + source
                coverage.append(dict(
                    coverage_id=key, batch_key="fixture", task_id=key, source=source,
                    security=sid, source_state="complete", input_rows=1, mapped_rows=1,
                    quarantined_rows=0, coverage_json=json.dumps(dict(
                        source_status="parsed_provider_tables", counts={"actions": 0},
                        table_states={name: dict(state="provider_no_rows_reported", explicit_empty_marker=True)
                                      for name in ("sharebonus_1", "sharebonus_2")},
                    )),
                ))
                captures.append(dict(
                    capture_id=key, batch_key="fixture", task_id=key, source=source,
                    observed_at="2025-01-01T00:00:00+00:00", source_state="complete",
                ))
        for index, (table, rows) in enumerate({
            "meta.financial_coverage": coverage,
            "meta.financial_captures": captures,
            "corporate_action.events": actions,
        }.items()):
            schema, name = TABLES[table], f"refs{index}"
            c.execute("CREATE TABLE " + name + "(" + ",".join(f"{k} {v}" for k, v in schema.items()) + ")")
            if rows:
                c.executemany(
                    "INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in schema) + ")",
                    [[r.get(k) for k in schema] for r in rows],
                )
            item = writer.parquet(c, "SELECT * FROM " + name, [], name)
            ev.append(dict(
                item, kind="financial_table", table=table, rows=len(rows),
                partition_key="fixture", key_value_mismatches=0,
            ))
        c.close()
        contract = dict(
            schema="ad.financial-tables.v1", table_names=list(TABLES),
            candidate_ids=[], table_set_sha256=table_set_hash(ev),
        )
        with sqlite3.connect(root / "work/daily.sqlite3") as db:
            db.execute("CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT)")
            db.execute("INSERT INTO release_contracts VALUES('financial_reference_tables',?)", [json.dumps(contract)])
        db.close()
        meta = writer.object(root / "work/daily.sqlite3", "sqlite3")
        writer.publish(dict(
            metadata=meta, files=files, evidence=ev,
            financial_reference_tables=contract,
            financial_table_validation={"table_set_sha256": contract["table_set_sha256"]},
            input_dispositions={"constructed": {"input_rows": 1, "adopted": 1}},
        ), expected_parent=None)
    return MarketRelease(root), days


def test_release_derived_fq_pre_and_post_without_future_leakage(tmp_path, daily_jobs):
    from types import SimpleNamespace

    release, days = _split_release(tmp_path)
    app, jobs, body = daily_jobs
    jobs.product = SimpleNamespace(
        call=lambda f: f(SimpleNamespace(release=release)),
        configured=lambda: SimpleNamespace(document={"profiles": {"product": {}}}, profile="product"),
    )
    body.pop("securities")
    body.pop("warmup_sessions")
    # days[4] is the split ex-date (2024-03-08).
    # On days[4] open (previous_date = days[3]), future split on days[4] must NOT leak!
    # On days[5] open (previous_date = days[4]), pre-adjusted close of days[3] becomes 10.0,
    # and post-adjusted close of days[4] becomes 10.5 * (20.0 / 10.0) = 21.0.
    body.update(
        start_date=days[4],
        end_date=days[5],
        source="""from aquant import *
import math

def initialize(context):
    run_daily(check, time="open")

def check(context):
    d = str(context.current_dt.date())
    pre = attribute_history("600000.XSHG", 3, "1d", ["close", "volume", "money"])
    raw = attribute_history("600000.XSHG", 3, "1d", ["close", "volume", "money"], fq=None)
    post = get_price("600000.XSHG", count=3, fields=["close", "volume", "money"], fq="post")
    assert pre["volume"].equals(raw["volume"]) and post["money"].equals(raw["money"])
    if d == "2024-03-08":
        # Before split ex-date closes: no lookahead adjustment
        assert list(pre["close"]) == [20.0, 20.0, 20.0]
        assert list(post["close"]) == [20.0, 20.0, 20.0]
        # Cross-sectional history with missing-day stock & not-yet-listed stock -> NaN, no crash!
        panel = history(4, "1d", "close", ["600000.XSHG", "000001.XSHE", "600036.XSHG"])
        assert panel.shape == (4, 3)
        assert math.isnan(panel.loc["2024-03-06", "000001.XSHE"])
        assert panel["600036.XSHG"].isna().all()
    elif d == "2024-03-11":
        # After split ex-date (2024-03-08): prior 20.0 closes become 10.0 in fq='pre'
        assert [round(v, 4) for v in pre["close"]] == [10.0, 10.0, 10.5]
        # And in fq='post', 10.5 close on ex-date scales by 2.0 -> 21.0
        assert [round(v, 4) for v in post["close"]] == [20.0, 20.0, 21.0]
        panel = history(4, "1d", "close", ["600000.XSHG", "000001.XSHE", "600036.XSHG"])
        assert [round(v, 4) for v in panel["600000.XSHG"]] == [10.0, 10.0, 10.0, 10.5]
        assert panel["600036.XSHG"].iloc[:-1].isna().all()
        assert panel["600036.XSHG"].iloc[-1] == 30.0
        multi = get_price(["000001.XSHE", "600036.XSHG"], count=3, fields=["close"])
        assert set(multi["code"]) == {"000001.XSHE", "600036.XSHG"}
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")


@pytest.mark.parametrize(
    "fixed_release",
    [{"securities": [f"{600000 + i:06d}.XSHG" for i in range(25)]}],
    indirect=True,
)
def test_multistock_batch_prefetch_and_per_security_lru_avoid_cache_thrashing(
    daily_jobs, fixed_release, tmp_path, monkeypatch
):
    _, _, body = daily_jobs
    release, days = fixed_release
    sids = [f"{600000 + i:06d}.XSHG" for i in range(25)]
    body.update(securities=[], start_date=days[25], end_date=days[27])
    data = DemandInputs(release.receipt(), PythonCreate.model_validate(body), tmp_path)
    calls = []
    original_prices = data.owner.prices

    def tracked_prices(securities, start, end, *args, **kwargs):
        calls.append((list(securities), start, end))
        return original_prices(securities, start, end, *args, **kwargs)

    monkeypatch.setattr(data.owner, "prices", tracked_prices)
    try:
        # Day 1: batch prefetch 20-day window across 25 securities -> 1 batch call!
        w1 = days[5:25]
        data.prefetch_daily(sids, w1, history=True)
        for sid in sids:
            assert len(data._daily(sid, w1, history=True)) == 20
        assert len(calls) == 1 and len(calls[0][0]) == 25
        assert len(data.history_rows) == 25 * 20

        # Day 2: sliding window advances by 1 day -> only 1 new day fetched in 1 batch call!
        w2 = days[6:26]
        data.prefetch_daily(sids, w2, history=True)
        for sid in sids:
            assert len(data._daily(sid, w2, history=True)) == 20
        assert len(calls) == 2 and calls[1][1] == calls[1][2] == days[25]
        assert len(data.history_rows) == 25 * 20

        data.finalize()
        frozen = evidence(tmp_path)
        pairs = [(r["security"], r["date"]) for r in frozen["daily"]]
        assert len(pairs) == len(set(pairs)) == 25 * 21
    finally:
        data.close()


def test_contract_snapshots_exclude_volatile_owner_and_source_hashes():
    from pathlib import Path

    contracts_dir = Path(__file__).resolve().parents[2] / "docs/contracts"
    identity = json.loads((contracts_dir / "identity.json").read_text())
    assert "source_sha256" not in identity
    openapi = json.loads((contracts_dir / "api.openapi.json").read_text())
    owner_props = openapi["components"]["schemas"]["ResearchOwner"]["properties"]
    for field in ("version", "commit", "tree", "wheel_sha256"):
        assert "default" not in owner_props[field]
    runtime_owner = ResearchOwner()
    assert runtime_owner.version == ASHARE_VERSION
    assert runtime_owner.commit == ASHARE_COMMIT
    assert runtime_owner.tree == ASHARE_TREE
    assert runtime_owner.wheel_sha256 == ASHARE_WHEEL_SHA256
