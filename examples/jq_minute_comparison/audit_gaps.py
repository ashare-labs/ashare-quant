"""Audit one real JQ gap experiment without claiming native policy equivalence.

Daily volume is consulted only by this *post-run* verifier. It must never enter
the strategy's knowledge clock or silently replace the native participation rule.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from examples.jq_minute_comparison.align import captures, compare_aligned
from examples.jq_minute_comparison.compare import near, number, read_rows, require, timestamp

CASE = "minute-gaps-r4-v1"
STOCKS = ("600000.XSHG", "000001.XSHE")
DAYS = ("2026-10-09", "2026-10-12")
RATIO = Decimal("0.0001")
SOURCE_SHA256 = "15fd3ba5759bd5d05915aba7edd53569986c2643a64a2e1067afd33e926ea6e0"
LOG_SHA256 = "7a7b71103140ebe0f23e995c7695f1da7e093bcd1f754ab941437a491da343ed"
TDX_SHA256 = "6b3bb6fb89001ca5ceedf9b5f1171a107b4d1c324cb4dfdeb5b4b03f3621b02e"
BOUNDARIES = (
    "09:30",
    "09:31",
    "09:35",
    "09:40",
    "11:29",
    "11:30",
    "12:00",
    "13:00",
    "13:01",
    "13:02",
    "14:56",
    "14:57",
    "14:59",
    "15:00",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_capture(root):
    root = Path(root)
    local = Path(__file__).with_name("gap_probe.py")
    require(sha(local) == SOURCE_SHA256, "Gap probe source changed")
    for name in ("probe.uploaded.py", "probe.editor-readback.py", "probe.backtest-code.py"):
        require((root / name).read_bytes() == local.read_bytes(), "JQ gap source mismatch")
    meta = json.loads((root / "probe.run.json").read_text())
    require(
        meta["status"] == "completed"
        and meta["terminal_complete"]
        and meta["stored_script_equals_upload"]
        and meta["attempts"] == 1
        and meta["successful_runs"] == 1
        and meta["failed_runs"] == 0,
        "Incomplete gap reference",
    )
    rows = read_rows(root / "probe.logs.jsonl")
    require(sha(root / "probe.logs.jsonl") == LOG_SHA256, "Frozen gap log changed")
    require(len(rows) == meta["records"] == 165, "Gap reference row count")
    return rows


def clock_range(start, count):
    first = datetime.fromisoformat("2000-01-01T" + start)
    return [(first + timedelta(minutes=i)).strftime("%H:%M") for i in range(count)]


def audit_trace(rows, source):
    """Reconcile every account against causal trades, including odd-lot partials."""
    # Native/analytics imports may lower the process-wide Decimal precision.
    # Keep the independent ledger deterministic without changing their context.
    with localcontext() as context:
        context.prec = 50
        return _audit_trace(rows, source)


def _audit_trace(rows, source):
    require([r["seq"] for r in rows] == list(range(1, len(rows) + 1)), "Gap sequence")
    require(all(r["case"] == CASE for r in rows), "Gap identity")
    require(rows[0]["kind"] == "identity" and rows[-1]["kind"] == "complete", "Gap terminal")
    require(rows[-1]["days"] == 2, "Gap incomplete days")
    require(
        all(timestamp(a["time"]) <= timestamp(b["time"]) for a, b in zip(rows, rows[1:])),
        "Gap time order",
    )
    identity = rows[0]
    require(
        identity["dates"] == list(DAYS)
        and identity["stocks"] == list(STOCKS)
        and identity["cash"] == 10000000
        and number(identity["ratio"]) == RATIO
        and identity["run_params"]
        == dict(start_date=DAYS[0], end_date=DAYS[1], frequency="minute", type="full_backtest"),
        "Gap experiment settings",
    )
    require(not any(r["kind"].endswith("error") for r in rows), "Gap source query error")
    bars = {
        (sid, timestamp(b["source_label"])): b for sid, view in source.items() for b in view["rows"]
    }
    daily = {
        (r["security"], r["time"][:10]): r["rows"][0] for r in rows if r["kind"] == "closed_daily"
    }
    require(set(daily) == {(s, d) for s in STOCKS for d in DAYS}, "Gap daily coverage")
    minute = {}
    volumes = {}
    for row in rows:
        if row["kind"] != "market":
            continue
        for sid, view in row["securities"].items():
            require("history_error" not in view, "Gap minute query error")
            for bar in view["history"]:
                at = timestamp(bar["label"])
                require(at <= timestamp(row["time"]), "Gap future visible history")
                key = (sid, at)
                require(key not in minute or minute[key] == bar, "Gap minute mutation")
                minute[key] = bar
                independent = bars[key]
                for field in ("open", "high", "low", "close"):
                    near(bar[field], independent[field], "Gap independent OHLC")
                delta = number(independent["volume"]) - number(bar["volume"])
                if delta:
                    volumes[key] = dict(
                        security=sid,
                        label=str(at),
                        jq=bar["volume"],
                        tdx=independent["volume"],
                        tdx_minus_jq=str(delta),
                    )

    orders, pending, trades, ledger = {}, None, {}, []
    cash = Decimal(10000000)
    quantities, basis = defaultdict(int), defaultdict(Decimal)
    today = defaultdict(int)
    notionals, filled = defaultdict(Decimal), defaultdict(int)
    active_day = None
    accounts_checked = 0
    capacity_checks = []
    for row in rows:
        at = timestamp(row["time"])
        if row["time"][:10] != active_day:
            active_day = row["time"][:10]
            today.clear()
        if row["kind"] == "submit":
            require(pending is None, "Gap nested submit")
            pending = row
            estimate = number(row["limit"] or row["current"]["last_price"]) * row["amount"]
            require(
                number(row["available_cash"]) > estimate * Decimal("1.001"),
                "Gap cash constraint confounds capacity",
            )
        elif row["kind"] == "return":
            require(
                pending is not None
                and row["label"] == pending["label"]
                and row["time"] == pending["time"],
                "Gap return binding",
            )
            order = row["order"]
            require(order is not None and order["order_id"] not in orders, "Gap order identity")
            require(
                order["security"] == pending["security"]
                and order["amount"] == 100000
                and order["is_buy"] is True
                and order["add_time"] == pending["time"],
                "Gap order submission causality",
            )
            cap = int(number(daily[order["security"], active_day]["volume"]) * RATIO / 100) * 100
            require(order["filled"] == cap < order["amount"], "Gap initial daily cap")
            require(
                order["status"] == ("canceled" if pending["limit"] is None else "filled"),
                "Gap partial order return status",
            )
            orders[order["order_id"]] = pending
            capacity_checks.append(
                dict(
                    label=row["label"],
                    kind="initial_per_order_daily",
                    amount=cap,
                    denominator=daily[order["security"], active_day]["volume"],
                    denominator_available_to_verifier_only=True,
                )
            )
            pending = None
        elif row["kind"] == "account":
            accounts_checked += 1
            seen = set()
            for trade in row["trades"]:
                tid, oid = trade["trade_id"], trade["order_id"]
                require(tid not in seen, "Gap duplicate trade in collection")
                seen.add(tid)
                require(oid in orders, "Gap orphan trade")
                plan = orders[oid]
                t = timestamp(trade["time"])
                require(timestamp(plan["time"]) <= t <= at, "Gap trade causal clock")
                if tid in trades:
                    require(trades[tid] == trade, "Gap trade mutation")
                    continue
                sid = trade["security"]
                require(sid == plan["security"], "Gap trade security")
                qty = trade["amount"]
                require(type(qty) is int and 0 < qty <= 100000 - filled[oid], "Gap fill quantity")
                if t == timestamp(plan["time"]):
                    require(filled[oid] == 0, "Gap duplicate initial fill")
                    cap = int(number(daily[sid, active_day]["volume"]) * RATIO / 100) * 100
                    require(qty == cap, "Gap initial fill cap")
                    near(trade["price"], plan["current"]["last_price"], "Gap initial price")
                else:
                    require(plan["limit"] is not None, "Gap canceled market remainder filled")
                    bar = minute[sid, t]
                    require(number(bar["low"]) < number(plan["limit"]), "Gap resting cross")
                    cap = int(number(bar["volume"]) * RATIO)
                    require(qty == min(cap, 100000 - filled[oid]), "Gap resting share cap")
                    near(trade["price"], plan["limit"], "Gap resting fill price")
                    capacity_checks.append(
                        dict(
                            kind="resting_per_minute_integer_share",
                            time=trade["time"],
                            amount=qty,
                            denominator=bar["volume"],
                            aq_whole_lot_cap=int(number(bar["volume"]) * RATIO / 100) * 100,
                        )
                    )
                notional = qty * number(trade["price"])
                old_fee = max(Decimal(5), notionals[oid] * Decimal("0.0003")) if filled[oid] else 0
                notionals[oid] += notional
                fee = max(Decimal(5), notionals[oid] * Decimal("0.0003")) - old_fee
                cash -= notional + fee
                quantities[sid] += qty
                basis[sid] += notional
                today[sid] += qty
                filled[oid] += qty
                trades[tid] = trade
                ledger.append(
                    dict(
                        **trade,
                        first_observed_seq=row["seq"],
                        first_observed_at=row["time"],
                        fee=str(fee),
                        cash=str(cash),
                    )
                )
            require(
                seen == {tid for tid, t in trades.items() if t["time"][:10] == active_day},
                "Gap incomplete daily trade collection",
            )
            current_orders = {o["order_id"]: o for o in row["orders"]}
            require(
                set(current_orders)
                == {oid for oid, p in orders.items() if p["time"][:10] == active_day},
                "Gap order collection",
            )
            expected_open, locked = set(), Decimal(0)
            for oid, order in current_orders.items():
                plan = orders[oid]
                require(
                    order["add_time"] == plan["time"]
                    and order["filled"] == filled[oid]
                    and order["amount"] == 100000
                    and order["security"] == plan["security"]
                    and order["is_buy"] is True,
                    "Gap order ledger mismatch",
                )
                near(
                    order["commission"],
                    max(Decimal(5), notionals[oid] * Decimal("0.0003")),
                    "Gap cumulative order commission",
                )
                if order["status"] != "canceled":
                    require(
                        plan["limit"] is not None and order["status"] == "filled",
                        "Gap unexpected resting status",
                    )
                    expected_open.add(str(oid))
                    # Observed JQ reservation in this experiment: original
                    # requested limit notional minus cumulative fills and fee.
                    locked += (
                        100000 * number(plan["limit"])
                        - notionals[oid]
                        - max(Decimal(5), notionals[oid] * Decimal("0.0003"))
                    )
            require(set(row["open_orders"]) == expected_open, "Gap open order collection")
            positions = {p["security"]: p for p in row["positions"]}
            require(
                set(positions) == {sid for sid, q in quantities.items() if q}, "Gap position set"
            )
            value = Decimal(0)
            for sid, p in positions.items():
                qty = quantities[sid]
                require(
                    p["total_amount"] == qty
                    and p["today_amount"] == today[sid]
                    and p["closeable_amount"] == qty - today[sid],
                    "Gap quantity/T1 ledger",
                )
                near(p["avg_cost"], basis[sid] / qty, "Gap position cost")
                if row["time"][11:16] == "09:30":
                    # Opening quote comparison only. This does NOT make a future
                    # 09:31 interval available to a strategy at 09:30.
                    mark = bars[sid, at + timedelta(minutes=1)]["open"]
                else:
                    label = max(t for s, t in bars if s == sid and t <= at)
                    mark = bars[sid, label]["close"]
                near(p["price"], mark, "Gap independent position mark")
                near(p["value"], qty * number(mark), "Gap position valuation")
                value += qty * number(mark)
            a = row["account"]
            near(a["locked_cash"], locked, "Gap frozen cash ledger")
            near(a["available_cash"] + a["locked_cash"], cash, "Gap cash ledger")
            near(a["positions_value"], value, "Gap market value")
            near(a["total_value"], cash + value, "Gap account equity")
            near(a["starting_cash"], 10000000, "Gap initial cash")
    require(pending is None and len(orders) == 5 and len(trades) == 9, "Gap experiment completion")
    expected = clock_range("09:30", 120) + clock_range("13:00", 120)
    lifecycle = []
    for row in rows:
        if row["kind"] != "after_trading":
            continue
        require(row["handle_clocks"] == expected, "Gap full handle_data timeline")
        day = row["time"][:10]
        scheduled = [r for r in rows if r["kind"] == "scheduled" and r["time"].startswith(day)]
        require(
            tuple(r["clock"] for r in scheduled) == BOUNDARIES
            and all(r["clock"] == r["time"][11:16] for r in scheduled),
            "Gap scheduled boundary coverage",
        )
        for r in scheduled:
            require(
                r["handle_count"] == sum(c < r["clock"] for c in expected),
                "Gap schedule/handle ordering",
            )
        lifecycle.append(
            dict(
                date=day,
                handle_count=len(expected),
                first=expected[0],
                last=expected[-1],
                scheduled_without_handle=[
                    r["clock"] for r in scheduled if r["clock"] not in expected
                ],
            )
        )
    require(len(lifecycle) == 2, "Gap lifecycle days")
    close = next(r for r in rows if r["kind"] == "account" and r["time"] == DAYS[0] + " 15:30:00")
    next_open = next(
        r for r in rows if r["kind"] == "account" and r["time"] == DAYS[1] + " 09:00:00"
    )
    require(
        close["open_orders"] and close["account"]["locked_cash"] > 0, "Gap 15:30 expiry observation"
    )
    require(
        not next_open["open_orders"] and next_open["account"]["locked_cash"] == 0,
        "Gap next-day expiry observation",
    )
    return dict(
        accounts_checked=accounts_checked,
        trades=ledger,
        capacity_checks=capacity_checks,
        lifecycle=lifecycle,
        daily_volumes=[dict(security=s, date=d, **v) for (s, d), v in daily.items()],
        independent_OHLC_records=len(minute),
        volume_differences=list(volumes.values()),
        expiry_observation=dict(
            still_open_at=close["time"], gone_by=next_open["time"], exact_cancel_time_observed=False
        ),
        final_cash=str(cash),
        final_quantities=dict(quantities),
        strict_equivalence=False,
        historical_pit_verified=False,
    )


def audit_old_gaps(matrix, jq, source):
    with localcontext() as context:
        context.prec = 50
        return _audit_old_gaps(matrix, jq, source)


def _audit_old_gaps(matrix, jq, source):
    report = {}
    for name in ("shared_cash_t1", "visible_momentum"):
        root = Path(matrix) / (name + "-jq_current-resize")
        compared = compare_aligned(name, root, captures(jq, name), must_align=True)
        day = compared["daily_accounts"][0]
        components = []
        for p in day["aq_positions"]:
            sid = p["security"]
            independent = [r for r in source[sid]["rows"] if r["source_label"].startswith(DAYS[0])]
            earlier = next(
                r for r in independent if timestamp(r["source_label"]).strftime("%H:%M") == "14:56"
            )
            closing = next(
                r for r in independent if timestamp(r["source_label"]).strftime("%H:%M") == "15:00"
            )
            near(p["price"], earlier["close"], "Gap old input closing mark")
            q = next(x for x in day["jq_positions"] if x["security"] == sid)
            near(q["price"], closing["close"], "Gap old JQ closing mark")
            components.append(
                dict(
                    security=sid,
                    quantity=p["total_amount"],
                    earlier=earlier["close"],
                    closing=closing["close"],
                    delta=str(
                        p["total_amount"] * (number(earlier["close"]) - number(closing["close"]))
                    ),
                )
            )
        require(
            sum(number(c["delta"]) for c in components) == number(day["aq_minus_jq"]),
            "Gap valuation residual",
        )
        clocks = compared["lifecycle_observations"]
        a, j = (set(clocks[x]["handle_data_checkpoints"]) for x in ("aq", "jq"))
        report[name] = dict(
            valuation_components=components,
            valuation_delta=day["aq_minus_jq"],
            residual=day["difference_beyond_mark_clock"],
            old_checkpoint_counts=dict(aq=len(a), jq=len(j), common=len(a & j)),
            aq_only=sorted(a - j),
            jq_only=sorted(j - a),
            strict_equivalence=False,
        )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--old-jq", required=True, type=Path)
    p.add_argument("--old-matrix", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    rows = load_capture(args.capture)
    require(sha(args.source) == TDX_SHA256, "Frozen gap source export changed")
    source = json.loads(args.source.read_text())
    report = dict(
        real_jq=audit_trace(rows, source),
        old_gaps=audit_old_gaps(args.old_matrix, args.old_jq, source),
        new_jq_runs=1,
        new_native_runs=0,
        capture_sha256=sha(args.capture / "probe.logs.jsonl"),
        source_sha256=sha(args.source),
        runtime_change=False,
        daily_volume_cap_implemented=False,
        strict_equivalence=False,
    )
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps({k: v for k, v in report.items() if k not in {"real_jq", "old_gaps"}}, indent=2)
    )


if __name__ == "__main__":
    main()
