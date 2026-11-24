"""Regression tests for multi-stock cache eviction, deployment cmes_factors, and gap vs pause semantics."""

import json
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest
from ashare_data import Store
from ashare_data.financials.tables import TABLES
from ashare_data.providers.cmes import import_cmes_factors
from ashare_data.storage.financial_release import table_set_hash
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.data import ASHARE_COMMIT, ASHARE_TREE, ASHARE_VERSION, ASHARE_WHEEL_SHA256
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize
from ashare_quant.market.reader import ResearchOwner
from ashare_quant.tasks.minute.contracts import PythonCreate
from ashare_quant.tasks.minute.demand import DemandInputs
from ashare_quant.tasks.worker import work_one
from tests.minute_tasks.evidence import evidence
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


def _boundary_release(tmp_path):
    """Construct a release covering paused, newly-listed IPO, and missing-observation cases.

    Calendar days (8 sessions):
      0: 2024-03-04 (Mon)
      1: 2024-03-05 (Tue)
      2: 2024-03-06 (Wed)
      3: 2024-03-07 (Thu)
      4: 2024-03-08 (Fri)
      5: 2024-03-11 (Mon)
      6: 2024-03-12 (Tue)
      7: 2024-03-13 (Wed, following session)

    Securities:
      - 600000.XSHG (IPO 2020-01-01): normal active except confirmed paused (tradestatus='0', vol=0) on days[4]
      - 000001.XSHE (IPO 2020-01-01): normal active on all days[0..6]
      - 600036.XSHG (IPO 2024-03-08 = days[4]): no rows before days[4], active on days[4..6]
      - 000002.XSHE (IPO 2020-01-01): Mon days[0]=10.0, Tue days[1] MISSING, Wed days[2] preclose=12.0/close=12.0
    """
    days = ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07",
            "2024-03-08", "2024-03-11", "2024-03-12", "2024-03-13"]
    specs = [
        ("600000.XSHG", "sh600000", "2020-01-01"),
        ("000001.XSHE", "sz000001", "2020-01-01"),
        ("600036.XSHG", "sh600036", "2024-03-08"),
        ("000002.XSHE", "sz000002", "2020-01-01"),
    ]
    ids = [s[0] for s in specs]
    root = tmp_path / "boundary-market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        store = LocalDailyStore(root / "work")
        db = store.db
        db.executescript("""
            CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
            CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
            CREATE TABLE release_daily_summary(instrument_id INTEGER,source TEXT,rows INTEGER,first TEXT,last TEXT);
            CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT,release_id INTEGER,
                parser_sha256 TEXT,mapping_sha256 TEXT,body TEXT);
        """)
        db.execute(
            "INSERT INTO data_batches VALUES(1,'fixture',1,'fixture','fixture',?)",
            [json.dumps({"manifest": {"target_start": days[0], "target_end": days[-1]}})],
        )
        db.executemany("INSERT INTO calendar VALUES(?,'constructed',NULL)", [(d,) for d in days])
        for n, (sid, symbol, ipo) in enumerate(specs, 1):
            db.execute("INSERT INTO release_instruments VALUES(?,?,?)", [n, sid, "fixture"])
            db.execute("INSERT INTO release_identifiers VALUES(?,?)", [sid, n])
            db.execute(
                "INSERT INTO release_daily_summary VALUES(?,'constructed',?,?,?)",
                [n, len(days), max(days[0], ipo), days[-2]],
            )
            db.execute(
                "INSERT INTO listing_facts VALUES(?,?,?,NULL,'{}','2025-01-01',NULL)",
                [sid, symbol, ipo],
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

        # 1. 600000.XSHG: confirmed suspended on days[4]
        r1 = []
        for i, d in enumerate(days[:-1]):
            cl = 20.0 + i
            if i == 4:
                prev_cl = 20.0 + 3
                r1.append((1, 1, d, prev_cl, prev_cl, prev_cl, prev_cl, 0, 0.0, 1,
                           "sh600000", "constructed", f"s1:{d}", None,
                           "2025-01-01T00:00:00+00:00", f"{prev_cl:.2f}", "0", "0"))
            else:
                pre = 20.0 + max(0, i - 1) if i != 5 else 23.0
                r1.append((1, 1, d, cl, cl + 0.2, cl - 0.2, cl, 100000, cl * 100000, 1,
                           "sh600000", "constructed", f"s1:{d}", None,
                           "2025-01-01T00:00:00+00:00", f"{pre:.2f}", "1", "0"))

        # 2. 000001.XSHE: active all days
        r2 = [
            (1, 2, d, 15.0 + i * 0.1, 15.5 + i * 0.1, 14.5 + i * 0.1, 15.0 + i * 0.1,
             100000, (15.0 + i * 0.1) * 100000, 1, "sz000001", "constructed", f"s2:{d}", None,
             "2025-01-01T00:00:00+00:00", f"{15.0 + max(0, i - 1) * 0.1:.2f}", "1", "0")
            for i, d in enumerate(days[:-1])
        ]

        # 3. 600036.XSHG: IPO on days[4] (2024-03-08), only days[4..6] exist
        r3 = [
            (1, 3, d, 30.0 + (i - 4), 30.5 + (i - 4), 29.5 + (i - 4), 30.0 + (i - 4),
             100000, (30.0 + (i - 4)) * 100000, 1, "sh600036", "constructed", f"s3:{d}", None,
             "2025-01-01T00:00:00+00:00", f"{30.0 + max(0, i - 5):.2f}", "1", "0")
            for i, d in enumerate(days[:-1])
            if i >= 4
        ]

        # 4. 000002.XSHE: Reviewer case #2 — Mon=10, Tue(days[1]) MISSING, Wed(days[2]) preclose=12/close=12
        r4 = []
        for i, d in enumerate(days[:-1]):
            if i == 1:
                continue  # Unexplained missing trading day!
            cl = 10.0 if i == 0 else 12.0
            pre = 10.0 if i == 0 else 12.0
            r4.append((1, 4, d, cl, cl + 0.1, cl - 0.1, cl, 100000, cl * 100000, 1,
                       "sz000002", "constructed", f"s4:{d}", None,
                       "2025-01-01T00:00:00+00:00", f"{pre:.2f}", "1", "0"))

        for n, group in ((1, r1), (2, r2), (3, r3), (4, r4)):
            c.executemany(
                "INSERT INTO prices VALUES(" + ",".join("?" for _ in group[0]) + ")", group
            )
            item = writer.parquet(
                c, "SELECT * FROM prices WHERE instrument_id=?", [n], f"bars{n}"
            )
            files.append(dict(
                item, table="bars_1d", series_id=1, instrument_id=n,
                rows=len(group), first_date=group[0][2], last_date=group[-1][2], duplicate_keys=0,
            ))

        ev, coverage, captures, actions = [], [], [], []
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


@pytest.mark.parametrize(
    "fixed_release",
    [{"securities": [f"{600000 + i:06d}.XSHG" for i in range(4)]}],
    indirect=True,
)
def test_review_issue_1_full_cache_never_turns_cached_stock_into_nan_or_depends_on_order(
    daily_jobs, fixed_release, monkeypatch
):
    """Reproduce Reviewer Issue #1: full cache + [new_stock, oldest_cached] vs [oldest_cached, new_stock]."""
    monkeypatch.setattr(DemandInputs, "MAX_HISTORY_SECURITIES", 2)
    app, jobs, body = daily_jobs
    _, days = fixed_release
    body.pop("securities")
    body.pop("warmup_sessions")
    body.update(
        start_date=days[10],
        end_date=days[10],
        source="""from aquant import *

def initialize(context):
    run_daily(check, time="open")

def check(context):
    # Fill the 2-slot cache with 600000 (oldest) and 600001 (newest)
    h_init = history(3, "1d", "close", ["600000.XSHG", "600001.XSHG"], fq=None)
    assert not h_init.isna().any().any()

    # Now query [new_stock 600002, oldest_cached 600000] -> must NEVER evict 600000 into NaN!
    h_a = history(3, "1d", "close", ["600002.XSHG", "600000.XSHG"], fq=None)
    assert not h_a.isna().any().any()

    # Re-seed [600000, 600001] and query in swapped order [600000, 600002] -> identical values!
    history(3, "1d", "close", ["600000.XSHG", "600001.XSHG"], fq=None)
    h_b = history(3, "1d", "close", ["600000.XSHG", "600002.XSHG"], fq=None)
    assert h_a["600000.XSHG"].equals(h_b["600000.XSHG"])
    assert h_a["600002.XSHG"].equals(h_b["600002.XSHG"])

    # Query 4 stocks at once (> MAX_HISTORY_SECURITIES=2) -> all 4 have complete prices!
    h_all = history(3, "1d", "close",
                    ["600000.XSHG", "600001.XSHG", "600002.XSHG", "600003.XSHG"], fq=None)
    assert h_all.shape == (3, 4) and not h_all.isna().any().any()
    p_all = get_price(["600003.XSHG", "600000.XSHG"], count=3, fields=["close"], fq=None)
    assert len(p_all) == 6 and not p_all["close"].isna().any()
""",
    )
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")


def test_review_issue_2_missing_day_never_invents_pre_adjustment_and_deployment_shares_cmes_factors(
    tmp_path,
):
    """Reproduce Reviewer Issue #2 + Reminder #1: no preclose guessing; deployment.json cmes_factors shared."""
    release, days = _boundary_release(tmp_path)

    # 1. Without cmes_factors in deployment.json, fq='pre' fails explicitly (never guesses 12.0 from preclose)
    factor_store_path = tmp_path / "ad-store"
    store = Store.init(factor_store_path)
    paths = []
    for mode, initial, updated in (("hfq", 1, 2), ("qfq", 2, 1)):
        zpath = tmp_path / f"{mode}.zip"
        with zipfile.ZipFile(zpath, "w") as archive:
            archive.writestr(
                "SH.600000.csv", f"date,{mode}_factor\n1900-01-01,{initial}\n2024-03-07,{updated}\n"
            )
            archive.writestr(
                "SZ.000001.csv", f"date,{mode}_factor\n1900-01-01,1\n2024-03-07,1\n"
            )
            archive.writestr(
                "SZ.000002.csv", f"date,{mode}_factor\n1900-01-01,1\n2024-03-07,1\n"
            )
        paths.append(zpath)
    dataset_id = import_cmes_factors(
        store,
        hfq_path=paths[0],
        qfq_path=paths[1],
        members={
            "600000.XSHG": "SH.600000.csv",
            "000001.XSHE": "SZ.000001.csv",
            "000002.XSHE": "SZ.000002.csv",
        },
        through="2024-03-11",
    )

    deploy_doc = {
        "schema": "ashare-deployment.v1",
        "data_root": str(tmp_path),
        "assets": {
            "releases": {"kind": "store", "path": "ad-store", "role": "publication", "retention": "preserve"},
            "market": {"kind": "market_release", "path": "boundary-market", "role": "market", "retention": "preserve"},
        },
        "profiles": {
            "product": {
                "kind": "product",
                "release_store_asset": "releases",
                "market_release": {"asset": "market", "release_id": release.release_id, "manifest_sha256": release.sha256},
                "cmes_factors": {"asset": "releases", "dataset_id": dataset_id},
                "control_db": "state/control.sqlite3",
                "paper_root": "state/paper",
                "reference_sets": {"ref": {"kind": "market_release"}},
                "default_reference_set": "ref",
            }
        },
    }
    deploy_file = tmp_path / "deployment.json"
    deploy_file.write_text(json.dumps(deploy_doc))
    config = resolve(deploy_file)
    assert config.cmes_factors == {"root": str(factor_store_path.resolve()), "dataset_id": dataset_id}
    assert "releases" in config.selected_assets
    initialize(config)

    with TestClient(create_app(product_configuration=config, diagnostics_configuration={})) as client:
        # Shared across Python catalogue, Product config, and Factor sources!
        cat = client.get("/api/v1/python/catalogue").json()
        assert cat["daily"]["history_factors"]["dataset_id"] == dataset_id
        prod_cfg = client.get("/api/v1/product/config").json()
        assert prod_cfg["capabilities"]["cmes_factors"]["dataset_id"] == dataset_id
        factor_sources = client.get("/api/v1/factors/sources").json()["items"]
        mr_source = next(s for s in factor_sources if s["id"] == "market-release")
        assert mr_source["ready"] is True
        assert mr_source["evaluation_basis"] == "CMES pre-adjusted close; frozen factor dataset"

        # Submit backtest using fq='pre' anchored at visible date without future factor leakage:
        # Factor changes from 1 -> 2 on 2024-03-07 (days[3]).
        # On 2024-03-07 open (previous_date = 2024-03-06), 2024-03-07 factor must NOT leak!
        # On 2024-03-08 open (previous_date = 2024-03-07), days[1]=21.0 becomes 10.5, days[3]=23.0 stays 23.0!
        body = {
            "name": "Deployment factor check",
            "idempotency_key": "deploy-fq-1",
            "frequency": "1d",
            "start_date": "2024-03-07",
            "end_date": "2024-03-08",
            "source": """from aquant import *
def initialize(context):
    run_daily(check, time="open")
def check(context):
    d = str(context.current_dt.date())
    pre = attribute_history("600000.XSHG", 2, "1d", ["close"], fq="pre")
    raw = attribute_history("600000.XSHG", 2, "1d", ["close"], fq=None)
    if d == "2024-03-07":
        assert pre["close"].equals(raw["close"])
    elif d == "2024-03-08":
        assert round(float(pre["close"].iloc[0]), 4) == round(float(raw["close"].iloc[0]) / 2, 4)
        assert round(float(pre["close"].iloc[1]), 4) == round(float(raw["close"].iloc[1]), 4)
""",
        }
        resp = client.post("/api/v1/python/jobs", json=body)
        assert resp.status_code == 201, resp.text
        job_id = resp.json()["job_id"]
        # Frozen per backtest even if external store file is removed after submission
        (factor_store_path / "cmes_factors" / f"{dataset_id}.json").unlink()
        assert work_one(client.app.state.python_jobs, job_id) == job_id
        done = client.get(f"/api/v1/python/jobs/{job_id}").json()
        assert done["status"] == "succeeded", done.get("error")

        # Insufficient factor coverage (600036.XSHG not in factor snapshot, or date > through 2024-03-11) fails explicitly
        # Restore factor file for next submissions
        import_cmes_factors(
            store, hfq_path=paths[0], qfq_path=paths[1],
            members={
                "600000.XSHG": "SH.600000.csv",
                "000001.XSHE": "SZ.000001.csv",
                "000002.XSHE": "SZ.000002.csv",
            },
            through="2024-03-11",
        )
        # Reviewer Issue #2 exact case: Mon=10, Tue missing, Wed preclose=12/close=12 with factor=1 -> Mon MUST stay 10.0!
        mon_only = execute(client, client.app.state.python_jobs, {
            **body, "idempotency_key": "deploy-fq-mon-10",
            "start_date": "2024-03-07", "end_date": "2024-03-07",
            "source": "from aquant import *\ndef initialize(c): pass\ndef handle_data(c,d):\n    df = get_price('000002.XSHE', start_date='2024-03-04', end_date='2024-03-04', fields=['close'], fq='pre')\n    assert float(df['close'].iloc[0]) == 10.0\n",
        })
        assert mon_only["status"] == "succeeded", mon_only.get("error")

        bad_sec = execute(client, client.app.state.python_jobs, {
            **body, "idempotency_key": "deploy-fq-missing-sec",
            "start_date": "2024-03-11", "end_date": "2024-03-11",
            "source": "from aquant import *\ndef initialize(c): pass\ndef handle_data(c,d):\n    attribute_history('600036.XSHG', 1, fq='pre')\n",
        })
        assert bad_sec["status"] == "failed" and "CMES_FACTOR_MISSING" in bad_sec["error"]["message"]

        bad_date = execute(client, client.app.state.python_jobs, {
            **body, "idempotency_key": "deploy-fq-cutoff",
            "start_date": "2024-03-12", "end_date": "2024-03-12",
            "source": "from aquant import *\ndef initialize(c):\n    run_daily(check, time='after_close')\ndef check(c):\n    get_price('000001.XSHE', end_date='2024-03-12', count=1, fq='pre')\n",
        })
        assert bad_date["status"] == "failed" and "CMES_FACTOR_SCOPE" in bad_date["error"]["message"]

    # Unconfigured deployment profile: Mon=10 (Tue missing, Wed=12) never guesses 12.0; fails with AQUANT_ADJUSTMENT_UNAVAILABLE
    no_factor_doc = json.loads(json.dumps(deploy_doc))
    no_factor_doc["profiles"]["product"].pop("cmes_factors")
    no_factor_doc["profiles"]["product"]["control_db"] = "state-nofq/control.sqlite3"
    no_factor_doc["profiles"]["product"]["paper_root"] = "state-nofq/paper"
    no_factor_file = tmp_path / "deployment-nofq.json"
    no_factor_file.write_text(json.dumps(no_factor_doc))
    no_factor_cfg = resolve(no_factor_file)
    assert no_factor_cfg.cmes_factors is None
    initialize(no_factor_cfg)
    with TestClient(create_app(product_configuration=no_factor_cfg, diagnostics_configuration={})) as client:
        unconf = execute(client, client.app.state.python_jobs, {
            "name": "No factor check",
            "idempotency_key": "nofq-1",
            "frequency": "1d",
            "start_date": "2024-03-07",
            "end_date": "2024-03-07",
            "source": "from aquant import *\ndef initialize(c): pass\ndef handle_data(c,d):\n    get_price('000002.XSHE', start_date='2024-03-04', end_date='2024-03-04', fq='pre')\n",
        })
        assert unconf["status"] == "failed" and "AQUANT_ADJUSTMENT_UNAVAILABLE" in unconf["error"]["message"]


def test_review_issue_3_and_gap_vs_pause_vs_ipo_boundaries(tmp_path, daily_jobs):
    """Verify multi-stock get_price equivalence, pre-IPO/paused NaN in history(), and strict gap errors."""
    release, days = _boundary_release(tmp_path)
    app, jobs, body = daily_jobs
    jobs.product = SimpleNamespace(
        call=lambda f: f(SimpleNamespace(release=release)),
        configured=lambda: SimpleNamespace(document={"profiles": {"product": {}}}, profile="product"),
    )
    body.pop("securities")
    body.pop("warmup_sessions")

    with TestClient(app) as client:
        # 1. Reviewer Issue #3: count=2, skip_paused=True on 2024-03-12 (days[6], previous=days[5], days[4] paused for 600000)
        # Single-stock and multi-stock get_price MUST BOTH step back to days[3] and return 2 bars for 600000!
        # Meanwhile wide history(3, ...) on shared axis [days[3], days[4], days[5]] fills NaN on days[4] (paused)
        # and NaN on days[3] for 600036 (pre-IPO, since IPO=days[4]=2024-03-08)!
        ok_job = execute(client, jobs, {
            **body, "idempotency_key": "issue3-ok",
            "start_date": days[6], "end_date": days[6],
            "source": """from aquant import *
import math

def initialize(context):
    run_daily(check, time="open")

def check(context):
    single = get_price("600000.XSHG", count=2, fields=["close"], skip_paused=True, fq=None)
    multi = get_price(["600000.XSHG", "000001.XSHE"], count=2, fields=["close"], skip_paused=True, fq=None)
    m_600000 = multi[multi["code"] == "600000.XSHG"].set_index("time")
    assert len(single) == 2 and len(m_600000) == 2
    assert list(single.index) == list(m_600000.index)
    assert [str(d.date()) for d in single.index] == ["2024-03-07", "2024-03-11"]
    assert len(multi[multi["code"] == "000001.XSHE"]) == 2

    # Wide history() keeps shared calendar axis [03-07, 03-08, 03-11]:
    # - 600000.XSHG is confirmed paused on 03-08 -> NaN on 03-08
    # - 600036.XSHG IPOs on 03-08 -> NaN on 03-07 (pre-IPO), 30.0 on 03-08, 31.0 on 03-11
    panel = history(3, "1d", "close", ["600000.XSHG", "000001.XSHE", "600036.XSHG"], fq=None)
    assert [str(d.date()) for d in panel.index] == ["2024-03-07", "2024-03-08", "2024-03-11"]
    assert math.isnan(panel.loc["2024-03-08", "600000.XSHG"])
    assert panel.loc["2024-03-07", "600000.XSHG"] == 23.0
    assert math.isnan(panel.loc["2024-03-07", "600036.XSHG"])
    assert list(panel.loc[["2024-03-08", "2024-03-11"], "600036.XSHG"]) == [30.0, 31.0]
""",
        })
        assert ok_job["status"] == "succeeded", ok_job.get("error")

        # 2. Newly-listed stock 600036.XSHG (only 2 bars on 03-08, 03-11) queried with count=5 in get_price:
        # Must report AQUANT_INSUFFICIENT_VISIBLE_HISTORY ("上市历史不足"), NOT PYTHON_DAILY_GAP!
        ipo_short = execute(client, jobs, {
            **body, "idempotency_key": "ipo-short",
            "start_date": days[6], "end_date": days[6],
            "source": """from aquant import *
def initialize(c): pass
def handle_data(c, d):
    get_price(["000001.XSHE", "600036.XSHG"], count=5, skip_paused=True, fq=None)
""",
        })
        assert ipo_short["status"] == "failed"
        assert "AQUANT_INSUFFICIENT_VISIBLE_HISTORY" in ipo_short["error"]["message"] or "上市历史不足" in ipo_short["error"]["message"]
        assert "上市历史不足" in ipo_short["error"]["message"]
        assert "PYTHON_DAILY_GAP" not in ipo_short["error"]["message"]

        # 3. Unexplained post-listing missing day on 000002.XSHE (2024-03-05 missing):
        # Must fail with PYTHON_DAILY_GAP naming 000002.XSHE and 2024-03-05 in BOTH get_price(skip_paused=True) AND history()!
        for key, call in (
            ("gap-get-price", "get_price(['000001.XSHE', '000002.XSHE'], count=3, skip_paused=True, fq=None)"),
            ("gap-history", "history(3, '1d', 'close', ['000001.XSHE', '000002.XSHE'], fq=None)"),
        ):
            gap_job = execute(client, jobs, {
                **body, "idempotency_key": key,
                "start_date": days[3], "end_date": days[3],
                "source": f"from aquant import *\ndef initialize(c): pass\ndef handle_data(c,d):\n    {call}\n",
            })
            assert gap_job["status"] == "failed", key
            msg = gap_job["error"]["message"]
            assert "000002.XSHE" in msg and "2024-03-05" in msg, msg


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
        w1 = days[5:25]
        res1 = data.daily_batch(sids, w1, history=True)
        assert len(res1) == 25 and all(len(v) == 20 for v in res1.values())
        assert len(calls) == 1 and len(calls[0][0]) == 25
        assert len(data.history_rows) == 25 * 20

        w2 = days[6:26]
        res2 = data.daily_batch(sids, w2, history=True)
        assert len(res2) == 25 and all(len(v) == 20 for v in res2.values())
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
