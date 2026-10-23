"""Offline native company-action conformance against a pinned AD package.

Reads the existing LocalDailyStore with query_only and uses the public
CorporateHistoryView. It never acquires data. Seeded holdings are a declared
research premise, not evidence of historical executions. Price limits use an
explicit retrospective 5%/10% model, not an exchange-certified limit series.
All evidence and actual account runs stay outside the source repository.
"""

import argparse
import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ashare_data import CorporateHistoryView
from ashare_data.storage.local_daily import LocalDailyStore

from ashare_quant.portfolio.corporate_export import export
from ashare_quant.portfolio.inputs import digest

REPO = Path(__file__).resolve().parents[1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only-longi", action="store_true")
    parser.add_argument("--native-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    view = CorporateHistoryView(args.binding, expected_binding_sha256=args.binding_sha256)
    store = LocalDailyStore(args.library, readonly=True)
    store.db.execute("PRAGMA query_only=ON")
    store.db.execute("BEGIN")
    calendar = [r[0] for r in store.db.execute("SELECT date FROM calendar ORDER BY date")]
    dbstat = (args.library / "daily.sqlite3").stat()
    save(
        out / "sources.json",
        dict(
            binding_sha256=args.binding_sha256,
            database=str(args.library / "daily.sqlite3"),
            database_readonly=True,
            query_only=store.db.execute("PRAGMA query_only").fetchone()[0],
            database_size=dbstat.st_size,
            database_mtime_ns=dbstat.st_mtime_ns,
            public_reader_sha256=hashlib.sha256(
                Path(inspect.getfile(CorporateHistoryView)).read_bytes()
            ).hexdigest(),
        ),
    )
    manifest_template = json.loads((REPO / "tests/fixtures/research/portfolio/manifest.json").read_text())
    cases = [
        dict(
            name="longi-hold",
            security="601012.XSHG",
            start="2022-05-20",
            end="2022-06-17",
            targets=None,
        ),
        dict(
            name="longi-sell-old-then-new",
            security="601012.XSHG",
            start="2022-05-20",
            end="2022-06-17",
            targets={"79.9800": 400, "60.6200": 0},
        ),
        dict(
            name="longi-attempt-sell-locked",
            security="601012.XSHG",
            start="2022-05-20",
            end="2022-06-17",
            targets={"79.9800": 0, "60.6200": 0},
        ),
    ]
    if not args.only_longi:
        ordinary = view.export_aq(
            [
                "000001.XSHE",
                "600000.XSHG",
                "600036.XSHG",
                "000651.XSHE",
                "600519.XSHG",
                "000333.XSHE",
                "000858.XSHE",
                "601318.XSHG",
            ],
            start="2020-01-01",
            end="2025-12-31",
            tax_model="gross_cash_no_tax",
            accept_source_scoped_coverage=True,
        )
        assert ordinary["ready_for_declared_date_model"] and len(ordinary["rows"]) == 67
        for i, e in enumerate(ordinary["rows"]):
            cases.append(
                dict(
                    name=f"cash-{i + 1:02d}-{e['security'][:6]}-{e['ex_date']}",
                    security=e["security"],
                    start=calendar[calendar.index(e["record_date"]) - 1],
                    end=calendar[calendar.index(e["payment_date"]) + 1],
                    targets=None,
                )
            )
        cases.append(
            dict(
                name="suspended-cash",
                security="000022.XSHE",
                start="2018-05-02",
                end="2018-05-25",
                targets={"25.1600": 0},
            )
        )
    save(
        out / "coverage-plan.json",
        dict(
            cases=cases,
            rule="All 67 fixed ordinary cash events, LONGi separated credit/tradability and suspended A-share cash; no selection by returns",
            initial_holdings="1000 declared old shares, cash 0; native prior-close seed model",
            limits="Explicit retrospective 5%/10% price limit model; no PIT or JQ equivalence",
        ),
    )
    summaries, commands = [], []
    for case in cases:
        root = out / case["name"]
        root.mkdir()
        sid, start, end = case["security"], case["start"], case["end"]
        first, last = calendar.index(start), calendar.index(end)
        cal, days = calendar[first - 120 : last + 2], calendar[first : last + 1]
        frame = store.get_price(sid, start=cal[0], end=end, source="baostock")
        raw = json.loads(frame.to_json(orient="records"))
        rows = []
        for r in raw:
            rate = Decimal("0.05" if r["is_st"] == "1" else "0.10")
            assert r["is_st"] in ("0", "1") and r["tradestatus"] in ("0", "1")
            prior = Decimal(r["preclose"])
            row = dict(
                r,
                security=sid,
                suspended=r["tradestatus"] == "0",
                upper_limit=str(
                    (prior * (1 + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                ),
                lower_limit=str(
                    (prior * (1 - rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                ),
                limit_origin="AQ explicit retrospective 5%/10% model; not AD/exchange observed limits",
            )
            rows.append(row)
        by_day = {r["date"]: r for r in rows}
        assert set(cal[:-1]) <= set(by_day), (sid, "missing history")
        document = dict(
            schema="ad-daily-portfolio.v1",
            producer={"name": "ashare-data", "owner_api": "LocalDailyStore.get_price"},
            real_market_data=True,
            price_basis="raw",
            volume_unit="share",
            securities=[sid],
            calendar=cal,
            daily=rows,
            actions=[],
            action_coverage={"status": "unavailable"},
            listing_coverage="scoped_research_model",
            state_mode="retrospective_model",
            source_refs=[{"database": str(args.library), "response_sha256": digest(raw)}],
            owner_api_reads=[
                {
                    "operation": "LocalDailyStore.get_price",
                    "security": sid,
                    "start": cal[0],
                    "end": end,
                    "response_sha256": digest(raw),
                    "attributes": frame.attrs,
                }
            ],
            owner_history_sha256={
                day: digest(
                    {sid: [by_day[d]["close"] for d in cal[cal.index(day) - 120 : cal.index(day)]]}
                )
                for day in days
            },
            assumptions=[
                "Explicit retrospective 5%/10% price limits from source preclose/is_st; not historical official limits.",
                "1000 old shares seeded at previous close as research premise; no historical purchase claimed.",
            ],
            limitations=[
                "Non-PIT frozen source. Source-scoped company action coverage. No JQ equivalence."
            ],
        )
        document = export(
            document, args.binding, args.binding_sha256, start, end, accept_date_model=True
        )
        save(root / "input.json", document)
        manifest = copy.deepcopy(manifest_template)
        config = dict(
            id="daily-equal-weight",
            purpose="Dated corporate accounting conformance fixture; not investment strategy",
        )
        manifest.update(
            securities=[sid], windows=[dict(id="test", start=start, end=end)], strategies=[config]
        )
        manifest["execution"].update(
            initial_cash="0",
            initial_positions={sid: 1000},
            initial_position_model="research_seed_previous_close",
            unfilled="Native market-order remainder cancelled during execution",
        )
        save(root / "manifest.json", manifest)
        save(root / "daily-equal-weight/config.json", config)
        # Fixed conformance actions determined by the prior observed close. The
        # schedule is not delivered to the callback and no current price is read.
        plan = {
            by_day[calendar[calendar.index(d) - 1]]["close"]: (
                1400 if sid == "601012.XSHG" and d >= "2022-06-06" else 1000
            )
            for d in days
        }
        if case["targets"]:
            plan.update(case["targets"])
            # After liquidation keep target zero, without a mutable callback counter.
            for d in days:
                if sid == "601012.XSHG" and d > "2022-06-07":
                    plan[by_day[calendar[calendar.index(d) - 1]]["close"]] = 0
            if sid == "000022.XSHE":
                plan = {key: 0 for key in plan}
        (root / "daily-equal-weight/strategy.py").write_text(
            f"def on_decision(history, prior_equity):\n    return {{'targets': {{{sid!r}: {plan!r}[history[{sid!r}][-1]]}}}}\n"
        )
        command = [
            str(args.native_python),
            "-B",
            "-m",
            "tests.support.portfolio_engine",
            "--manifest",
            str(root / "manifest.json"),
            "--input",
            str(root / "input.json"),
            "--strategy",
            "daily-equal-weight",
            "--window",
            "test",
            "--output",
            str(root / "run"),
            "--invocation",
            "baseline",
            "--accept-owner-state-model",
        ]
        started = time.perf_counter()
        with (root / "native.log").open("w") as log:
            proc = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1"),
            )
            _, status, usage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
        elapsed = time.perf_counter() - started
        commands.append(
            dict(
                name=case["name"],
                command=command,
                exit_code=proc.returncode,
                elapsed_seconds=elapsed,
                child_peak_rss=usage.ru_maxrss,
                rss_unit="bytes" if sys.platform == "darwin" else "KiB",
                rss_scope="wait4 per native CLI process; OS child-resource accounting",
            )
        )
        (out / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        assert proc.returncode == 0, (case["name"], (root / "native.log").read_text()[-6000:])
        result = json.loads((root / "run/result.json").read_text())
        assert json.loads((root / "run/audit.json").read_text())["passed"]
        events = document["corporate_schedule"]["rows"]
        assert len(events) == 1
        event = events[0]
        gross = Decimal(event["cash_per_share"]) * 1000
        registered = result["corporate_entitlements"][sid][event["entitlement_key"]]
        assert registered["basis"] == 1000 and Decimal(registered["cash"]) == gross
        accounts = {a["date"]: a for a in result["accounts"]}
        if case["targets"] is None:
            assert not result["trades"]
            assert Decimal(result["final_account"]["cash"]) == gross
        if case["name"] == "longi-hold":
            for date, wealth, available in [
                ("2022-06-06", 85128, 1000),
                ("2022-06-07", 85142, 1400),
            ]:
                assert Decimal(accounts[date]["equity"]) == wealth
                assert accounts[date]["positions"][sid]["quantity"] == 1400
                assert accounts[date]["positions"][sid]["sellable"] == available
        if case["name"] == "longi-sell-old-then-new":
            assert [(t["date"], t["quantity"]) for t in result["trades"]] == [
                ("2022-06-06", 1000),
                ("2022-06-07", 400),
            ]
            expected = (
                Decimal(260)
                + 1000 * Decimal("57.72") * Decimal("0.9987")
                + 400 * Decimal("62.70") * Decimal("0.9987")
            )
            assert abs(Decimal(result["final_account"]["cash"]) - expected) < Decimal("0.000001")
        if case["name"] == "longi-attempt-sell-locked":
            assert sum(t["quantity"] for t in result["trades"] if t["date"] == "2022-06-06") <= 1000
        if case["name"] == "suspended-cash":
            assert gross == 1319 and not result["trades"]
            assert result["submissions"] and all(s["returned_none"] for s in result["submissions"])
            assert Decimal(result["final_account"]["cash"]) == 1319
        summaries.append(
            dict(
                case=case["name"],
                security=sid,
                input_sha256=hashlib.sha256((root / "input.json").read_bytes()).hexdigest(),
                native_identity=result["identity"],
                gross_cash=str(gross),
                trades=len(result["trades"]),
                final_account=result["final_account"],
                elapsed_seconds=elapsed,
                passed=True,
            )
        )
        (out / "summary.json").write_text(
            json.dumps(
                dict(passed=True, completed=len(summaries), cases=summaries),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        print(case["name"], "passed", flush=True)
    store.db.close()


if __name__ == "__main__":
    main()
