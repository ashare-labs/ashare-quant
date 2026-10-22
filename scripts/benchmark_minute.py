"""Deterministic bounded minute benchmark; synthetic data, no market connection.

Generate once, then run this same script/environment against each checkout with
--source. Each invocation is a fresh process; use new output paths on every run.
No result-directory reentry, OS cache clearing, alternative matcher or ledger.
"""

import argparse
import cProfile
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path


def generate(stocks, days):
    if not (1 <= stocks <= 20 and 1 <= days <= 60):
        raise ValueError("Use 1..20 stocks and 1..60 full trading days")
    calendar, day = [], date(2024, 1, 2)
    while len(calendar) < days + 2:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    ids = [f"{600000 + n:06}.XSHG" for n in range(stocks)]
    doc = dict(
        schema="ad-minute-backtest.v1", producer={"name": "synthetic-benchmark"},
        real_market_data=False, source_refs=["deterministic benchmark construction v1"],
        limitations=["Synthetic calendar, prices and state; no real-market or JQ evidence."],
        price_basis="raw", volume_unit="share", amount_unit="CNY",
        visibility="declared_available_at", securities=ids, calendar=calendar,
        days=calendar[1:-1], sessions={}, states=[], minute=[], actions=[],
        action_coverage={"status": "synthetic_known_no_actions", "evidence": "construction"},
    )
    previous = {sid: str(10 + si) for si, sid in enumerate(ids)}
    for di, day in enumerate(doc["days"]):
        intervals = [("09:30", "11:30"), ("13:00", "15:00")]
        doc["sessions"][day] = [[f"{day}T{a}:00+08:00", f"{day}T{b}:00+08:00"]
                                for a, b in intervals]
        for si, sid in enumerate(ids):
            doc["states"].append(dict(
                security=sid, date=day, listed=True, suspended=False, is_st=False,
                lower_limit="1", upper_limit="100", previous_close=previous[sid],
                available_at=day + "T08:00:00+08:00", evidence="synthetic state",
            ))
            for minute in range(240):
                start = datetime.fromisoformat(doc["sessions"][day][minute // 120][0])
                start += timedelta(minutes=minute % 120)
                end = start + timedelta(minutes=1)
                price = (Decimal(10 + si) + Decimal(di % 7) / 100
                         + Decimal((minute % 19) - 9) / 1000)
                close = price + Decimal("0.003")
                doc["minute"].append(dict(
                    security=sid, bar_start=start.isoformat(), bar_end=end.isoformat(),
                    available_at=end.isoformat(), open=str(price), high=str(price + Decimal("0.02")),
                    low=str(price - Decimal("0.02")), close=str(close), volume="10000",
                    amount=str(price * 10000), evidence=f"synthetic:{di}:{si}:{minute}",
                ))
            previous[sid] = str(close)
    return doc


def strategy(ids, trading):
    return f'''from jqdata import order_target, run_daily
IDS = {ids!r}
def initialize(context):
    context.day = 0
    run_daily(rebalance, "09:35")
def before_trading_start(context):
    context.day += 1
def rebalance(context):
    if {trading!r}:
        for sid in IDS:
            order_target(sid, 300 if context.day % 2 else 0)
def handle_data(context, data):
    for sid in IDS:
        assert data[sid].close > 0
'''


def rss(which):
    value = resource.getrusage(which).ru_maxrss
    return value / (1024 ** 2 if sys.platform == "darwin" else 1024)


def measure(fn):
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu, wall = time.process_time(), time.perf_counter()
    result = fn()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, dict(
        wall_s=time.perf_counter() - wall, cpu_self_s=time.process_time() - cpu,
        cpu_children_s=max(0.0, after.ru_utime + after.ru_stime - child.ru_utime - child.ru_stime),
        self_peak_rss_mib=rss(resource.RUSAGE_SELF),
        child_peak_rss_mib=rss(resource.RUSAGE_CHILDREN),
    )


def financial_digest(result):
    # Source identity must change when code changes. All other result fields,
    # including every order/trade/account and policy, must remain byte-equivalent.
    value = {k: v for k, v in result.items() if k != "identity"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def compare(left, right):
    reports = [json.loads((root / "metrics.json").read_text()) for root in (left, right)]
    for field in ("input_sha256", "strategy_sha256", "result_sha256", "events_sha256"):
        if reports[0][field] != reports[1][field]:
            raise ValueError(f"Benchmark mismatch: {field}")
    results = [json.loads((root / "result.json").read_text()) for root in (left, right)]
    for result in results:
        result.pop("identity", None)
    if results[0] != results[1]:
        raise ValueError("Full results differ beyond expected source identity")
    if (left / "run/events.jsonl").read_bytes() != (right / "run/events.jsonl").read_bytes():
        raise ValueError("Event bytes/order differ")
    print(json.dumps({"equal": True, "result_sha256": reports[0]["result_sha256"],
                      "events_sha256": reports[0]["events_sha256"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", type=Path)
    parser.add_argument("--stocks", type=int, default=3)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("load", "native", "e2e"), default="native")
    parser.add_argument("--trading", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--strategy", type=Path, help="Optional fixed strategy for boundary cases")
    parser.add_argument("--accept-research", action="store_true")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("BASELINE", "CANDIDATE"))
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    if args.generate:
        doc = generate(args.stocks, args.days)
        with args.generate.open("x") as stream:
            json.dump(doc, stream, separators=(",", ":"))
        print(json.dumps({"rows": len(doc["minute"]), "path": str(args.generate)}))
        return
    if not (args.source and args.input and args.output):
        parser.error("--source, --input and --output are required for a run")
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.source.resolve() / "src"))
    os.environ.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                      NUMEXPR_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")

    def imports():
        from ashare_quant.backtests.minute.native import run_native
        from ashare_quant.backtests.minute.runner import DEFAULTS, MinuteInputs, run_backtest
        return run_native, DEFAULTS, MinuteInputs, run_backtest

    (run_native, defaults, inputs, run_backtest), import_metric = measure(imports)
    report = dict(mode=args.mode, trading=args.trading, profiled=args.profile,
                  python=sys.version, machine=platform.machine(), imports=import_metric)
    report["source_commit"] = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True,
    ).strip()
    native_path = args.source / "src/ashare_quant/backtests/minute/native.py"
    report["native_source_sha256"] = hashlib.sha256(native_path.read_bytes()).hexdigest()
    report["lock_sha256"] = hashlib.sha256((args.source / "uv.lock").read_bytes()).hexdigest()
    accept = dict(accept_assumed=args.accept_research,
                  accept_state_replay=args.accept_research, accept_research=args.accept_research)
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    if args.mode == "e2e":
        # Only read the universe here; the timed public runner validates and
        # loads its frozen inputs in both parent and isolated engine child.
        doc = json.loads(args.input.read_bytes())
        ids, days, rows = doc["securities"], doc["days"], len(doc["minute"])
        del doc
        source = args.strategy.read_text() if args.strategy else strategy(ids, args.trading)
        path = args.output / "strategy.py"
        path.write_text(source)
        run, report["e2e"] = measure(lambda: run_backtest(
            path, args.input, args.output / "run", timeout=600, **accept,
        ))
        result = run["result"]
        assert run["native_run"]
    else:
        data, report["load"] = measure(lambda: inputs(args.input, **accept))
        data.validate_execution(defaults)
        ids, days, rows = data.ids, data.days, len(data.bars)
        source = args.strategy.read_text() if args.strategy else strategy(list(ids), args.trading)
        if args.mode == "native":
            root = args.output / "run"
            root.mkdir()
            result, report["native"] = measure(lambda: run_native(
                root, data, source.encode(), defaults, "benchmark",
            ))
        else:
            result = None
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.output / "profile.pstats"))
    report.update(stocks=len(ids), days=len(days), rows=rows,
                  strategy_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest())
    if result is not None:
        report.update(result_sha256=financial_digest(result), orders=len(result["orders"]),
                      trades=len(result["trades"]), final_account=result["final_account"])
        events = args.output / "run/events.jsonl"
        report["events_sha256"] = hashlib.sha256(events.read_bytes()).hexdigest()
        (args.output / "result.json").write_text(json.dumps(result, sort_keys=True))
    (args.output / "metrics.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
