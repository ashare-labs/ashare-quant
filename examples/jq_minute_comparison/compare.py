"""Audit captured JQ observations against native AQ results. No network or execution engine."""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

NAMES = ("shared_cash_t1", "visible_momentum")
INPUT_SHA256 = "8e9320af8ac8b5c51dc5ca67112199c8969fe3a34a71d1a20497d24a9d2c52b2"
SOURCES = {
    "shared_cash_t1": "0d152768b7d23659cbe90416692b55189558bae7f350328ba935cf23fa0cc9f7",
    "visible_momentum": "ba09560537c8dbf949a2db210437d8284e199c85114251e89975a6897d157ad4",
}
STOCKS = ["600000.XSHG", "000001.XSHE"]
WINDOW = ["2026-10-09", "2026-10-12"]
D = Decimal
TOLERANCE = D("0.0000001")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    result = D(str(value))
    require(result.is_finite(), "non-finite number")
    return result


def near(actual, expected, label):
    require(abs(number(actual) - number(expected)) < TOLERANCE, label)


def timestamp(value):
    # Both captured strategy clocks are Shanghai-local; AQ native adds +08:00.
    value = datetime.fromisoformat(value)
    require(value.utcoffset() in (None, timedelta(hours=8)), "unexpected timezone")
    return value.replace(tzinfo=None)


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def aq_observations(path):
    return [
        json.loads(e["message"].removeprefix("AQMIN_JSON "))
        for e in read_rows(path)
        if e["kind"] == "log" and e["message"].startswith("AQMIN_JSON ")
    ]


def validate_rows(rows, name, platform):
    require(
        rows and [r["seq"] for r in rows] == list(range(1, len(rows) + 1)),
        "missing or duplicate observation sequence",
    )
    require(all(r["strategy"] == name for r in rows), "mixed strategies")
    require(
        [timestamp(r["time"]) for r in rows] == sorted(timestamp(r["time"]) for r in rows),
        "observation clock moved backwards",
    )
    identity = rows[0]
    for key, value in dict(
        kind="identity",
        platform=platform,
        case=name,
        source_sha256=SOURCES[name],
        stocks=STOCKS,
        window=WINDOW,
        initial_cash=100000,
        commission_rate=0.0003,
        min_commission=5,
        sell_tax=0.0005,
        slippage=0,
        volume_ratio=0.25,
        assertions="observed_without_abort",
    ).items():
        require(identity.get(key) == value, "identity mismatch: " + key)
    params = identity["run_params"]
    require(
        [params["start_date"], params["end_date"]] == WINDOW and params["frequency"] == "minute",
        "run parameters mismatch",
    )
    if platform == "jq":
        require(
            rows[-1]["kind"] == "complete" and rows[-2]["label"] == "strategy_end",
            "missing JQ terminal observations; completed UI may precede log flush",
        )
        require(rows[-1]["last_snapshot_sequence"] == rows[-2]["seq"], "bad terminal sequence")
    for row in rows:
        if row["kind"] == "snapshot":
            audit_snapshot(row)


def audit_snapshot(row):
    a, positions = row["account"], row["positions"]
    value = sum((number(p["value"]) for p in positions), D(0))
    near(a["positions_value"], value, "position value sum mismatch")
    near(
        a["total_value"],
        number(a["available_cash"]) + number(a["locked_cash"]) + value,
        "cash/equity identity mismatch",
    )
    require(number(a["available_cash"]) >= -TOLERANCE, "negative available cash")
    for p in positions:
        near(p["value"], number(p["total_amount"]) * number(p["price"]), "position mark mismatch")
        require(0 <= p["closeable_amount"] <= p["total_amount"], "invalid closeable quantity")


def fees(amount, price, is_buy):
    turnover = number(amount) * number(price)
    return max(D(5), turnover * D("0.0003")) + (D(0) if is_buy else turnover * D("0.0005"))


def audit_jq(rows, *, with_checks=False):
    """Bind immediate complete fills to calls, then reconcile EVERY snapshot.

    A missing trade collection is a diagnostic omission, never permission to
    skip account checks. Partial fills, inherited positions and actions remain
    outside this captured comparison's explicitly bounded scope.
    """
    validate_rows(rows, rows[0]["case"], "jq")
    orders, trades, trade_orders = {}, {}, {}
    positions, costs, bought_today = defaultdict(int), defaultdict(D), defaultdict(int)
    cash, ledger, checkpoints, first_observations = D(100000), [], [], []
    pending, day = None, None
    for row in rows:
        at = timestamp(row["time"])
        if at.date() != day:
            day = at.date()
            bought_today.clear()
        if row["kind"] == "submit":
            require(pending is None, "JQ submission without prior return")
            pending = row
        elif row["kind"] == "order_return":
            require(pending is not None, "JQ return without submission")
            require(
                all(row[k] == pending[k] for k in ("time", "api", "security")),
                "JQ return differs from pending submission",
            )
            pending = None
            order = row["order"]
            require(row["returned_none"] is (order is None), "JQ inconsistent return flag")
            if order is None:
                continue
            oid, sid, amount = str(order["order_id"]), order["security"], order["filled"]
            require(oid not in orders, "JQ duplicate order return")
            require(timestamp(order["add_time"]) == at, "JQ order time differs from submit/return")
            require(sid == row["security"] and sid in STOCKS, "JQ returned security differs")
            require(
                order["status"] == "held"
                and type(amount) is int
                and amount > 0
                and amount % 100 == 0
                and order["amount"] == amount,
                "JQ audit only supports immediate complete fills",
            )
            require(type(order["is_buy"]) is bool, "JQ invalid order direction")
            price = number(order["price"])
            require(price > 0, "JQ nonpositive execution price")
            charge = fees(amount, price, order["is_buy"])
            near(order["commission"], charge, "JQ combined commission/tax mismatch")
            gross = amount * price
            if order["is_buy"]:
                near(order["avg_cost"], price, "JQ buy order cost differs from its fill")
                positions[sid] += amount
                costs[sid] += gross
                bought_today[sid] += amount
                cash -= gross
            else:
                require(amount <= positions[sid] - bought_today[sid], "JQ T+1 sale violation")
                near(
                    order["avg_cost"], costs[sid] / positions[sid], "JQ sell holding cost mismatch"
                )
                costs[sid] -= costs[sid] * amount / positions[sid]
                positions[sid] -= amount
                cash += gross
            cash -= charge
            require(cash >= -TOLERANCE, "JQ negative ledger cash")
            orders[oid] = order
            ledger.append(
                dict(
                    time=order["add_time"],
                    security=sid,
                    side="BUY" if order["is_buy"] else "SELL",
                    amount=amount,
                    price=order["price"],
                    fees=str(charge),
                    cash_after=str(cash),
                    order_id=oid,
                )
            )
        if row["kind"] != "snapshot":
            continue
        account = row["account"]
        near(account["available_cash"], cash, "JQ trade-to-cash reconciliation mismatch")
        near(account["locked_cash"], 0, "JQ immediate-fill snapshot has locked cash")
        require(not row["open_orders"], "JQ immediate-fill snapshot has pending orders")
        held = {p["security"]: p for p in row["positions"]}
        require(len(held) == len(row["positions"]), "JQ duplicate position snapshot")
        require(
            {s: p["total_amount"] for s, p in held.items()}
            == {s: q for s, q in positions.items() if q},
            "JQ trade-to-position mismatch",
        )
        for sid, p in held.items():
            require(p["today_amount"] == bought_today[sid], "JQ today inventory mismatch")
            require(
                p["closeable_amount"] == positions[sid] - bought_today[sid],
                "JQ T+1 closeable mismatch",
            )
            near(p["avg_cost"], costs[sid] / positions[sid], "JQ holding cost mismatch")
        checkpoints.append(
            dict(
                seq=row["seq"],
                time=row["time"],
                label=row["label"],
                cash=str(cash),
                quantities={s: q for s, q in positions.items() if q},
                no_trade_collection=row["trades"] is None,
            )
        )
        todays = {oid: o for oid, o in orders.items() if timestamp(o["add_time"]).date() == day}
        if row["orders"] is not None:
            seen = set()
            for order in row["orders"]:
                oid = str(order["order_id"])
                require(oid not in seen and oid in todays, "JQ duplicate/unknown snapshot order")
                require(order == todays[oid], "JQ snapshot order changed after return")
                require(timestamp(order["add_time"]) <= at, "JQ order observed before submission")
                seen.add(oid)
            # The captured terminal callback clears both collections; allow only
            # that explicit lifecycle boundary, not an arbitrary missing set.
            expected = set() if row["label"] == "strategy_end" else set(todays)
            require(seen == expected, "JQ incomplete daily order collection")
        if row["trades"] is not None:
            seen, seen_orders = set(), set()
            for trade in row["trades"]:
                tid, oid = str(trade["trade_id"]), str(trade["order_id"])
                require(tid not in seen and oid not in seen_orders, "JQ duplicate snapshot trade")
                require(oid in todays, "JQ trade without a prior returned order")
                order = todays[oid]
                require(
                    timestamp(trade["time"]) == timestamp(order["add_time"]) <= at,
                    "JQ trade precedes submission or follows first observation",
                )
                require(
                    trade["security"] == order["security"] and trade["amount"] == order["filled"],
                    "JQ order/trade quantity or security mismatch",
                )
                near(trade["price"], order["price"], "JQ order/trade price mismatch")
                if tid in trades:
                    require(trades[tid] == trade, "trade mutated after first observation")
                else:
                    require(
                        oid not in trade_orders,
                        "JQ multiple trades for a complete single-fill order",
                    )
                    require(
                        timestamp(trade["time"]) == at, "JQ synchronous fill first observed late"
                    )
                    first_observations.append(
                        dict(
                            order_id=oid,
                            trade_id=tid,
                            seq=row["seq"],
                            submitted_at=order["add_time"],
                            observed_at=row["time"],
                        )
                    )
                    trades[tid], trade_orders[oid] = trade, tid
                seen.add(tid)
                seen_orders.add(oid)
            expected = set() if row["label"] == "strategy_end" else set(todays)
            require(seen_orders == expected, "JQ incomplete daily trade collection")
    require(pending is None, "JQ missing order return")
    require(len(orders) == len(trades) == len(ledger), "accepted order without observed trade")
    require(
        not any(positions.values()) and not rows[-2]["open_orders"], "JQ final account not flat"
    )
    near(rows[-2]["account"]["total_value"], cash, "JQ final equity mismatch")
    if with_checks:
        return dict(
            ledger=ledger,
            snapshots_checked=len(checkpoints),
            checkpoints=checkpoints,
            first_trade_observations=first_observations,
            no_trade_collection_snapshots=sum(c["no_trade_collection"] for c in checkpoints),
        )
    return ledger


def audit_aq(result, source):
    from examples.tdx_minute_research.run_demo import economic_audit

    economic_audit(result, source)
    require(
        result["historical_pit_verified"] is False and not result["is_test_double"],
        "unexpected native result provenance",
    )
    return [
        dict(
            time=t["at"],
            security=t["security"],
            side=t["side"],
            amount=t["amount"],
            price=t["price"],
            fees=str(number(t["commission"]) + number(t["tax"])),
        )
        for t in result["trades"]
    ]


def order_plan(row):
    style = row["style"]
    limit = (
        None
        if style is None
        else next(v for v in (style["price"], style["_limit_price"]) if isinstance(v, (int, float)))
    )
    return dict(
        time=row["time"],
        api=row["api"],
        security=row["security"],
        value=row["value"],
        limit=limit,
        current_price=row["market"]["last_price"],
    )


def compare_case(name, aq_rows, jq_rows, result, input_path):
    validate_rows(aq_rows, name, "aq")
    validate_rows(jq_rows, name, "jq")
    jq_audit = audit_jq(jq_rows, with_checks=True)
    aq_ledger, jq_ledger = audit_aq(result, input_path), jq_audit["ledger"]
    plans = [[order_plan(r) for r in rows if r["kind"] == "submit"] for rows in (aq_rows, jq_rows)]
    require(plans[0] == plans[1], "business order plans differ")
    histories = [
        [
            {k: r[k] for k in ("time", "labels", "close", "scores")}
            for r in rows
            if r["kind"] == "signal_history"
        ]
        for rows in (aq_rows, jq_rows)
    ]
    require(histories[0] == histories[1], "signal histories differ")
    source = json.loads(Path(input_path).read_text())
    bars = {(b["security"], timestamp(b["source_label"])): b for b in source["minute"]}
    for plan in plans[1]:
        near(
            plan["current_price"],
            bars[plan["security"], timestamp(plan["time"])]["close"],
            "JQ submission quote differs from independent TDX close",
        )
    for row in histories[1]:
        require(len(row["labels"]) == 5 and len(set(row["labels"])) == 5, "history length mismatch")
        require(
            all(timestamp(t) <= timestamp(row["time"]) for t in row["labels"]), "future history"
        )
        for sid, closes in row["close"].items():
            require(len(closes) == 5, "history close length mismatch")
            for time, price in zip(row["labels"], closes, strict=True):
                near(price, bars[sid, timestamp(time)]["close"], "history source mismatch")
            near(
                row["scores"][sid],
                number(closes[-1]) / number(closes[0]) - 1,
                "signal score mismatch",
            )
    for fill in jq_ledger:
        near(
            fill["price"],
            bars[fill["security"], timestamp(fill["time"])]["close"],
            "JQ fill differs from submission-minute close",
        )
    totals = []
    for ledger in (aq_ledger, jq_ledger):
        totals.append(
            {
                "gross_pnl": sum(
                    (
                        number(t["amount"])
                        * number(t["price"])
                        * (1 if t["side"] == "SELL" else -1)
                        for t in ledger
                    ),
                    D(0),
                ),
                "fees": sum((number(t["fees"]) for t in ledger), D(0)),
            }
        )
    delta = number(result["final_account"]["total_value"]) - number(
        jq_rows[-2]["account"]["total_value"]
    )
    gross_delta = totals[0]["gross_pnl"] - totals[1]["gross_pnl"]
    fee_delta = totals[1]["fees"] - totals[0]["fees"]
    near(delta, gross_delta + fee_delta, "unexplained final equity difference")
    snapshots = [
        [r for r in rows if r["kind"] == "snapshot" and r["time"].endswith("15:30:00")]
        for rows in (aq_rows, jq_rows)
    ]
    require(
        all([r["time"][:10] for r in group] == WINDOW for group in snapshots),
        "missing after-close accounts",
    )
    daily = []
    for aq, jq in zip(*snapshots, strict=True):
        daily.append(
            dict(
                date=aq["time"][:10],
                aq=aq["account"],
                jq=jq["account"],
                aq_positions=aq["positions"],
                jq_positions=jq["positions"],
                aq_minus_jq=str(
                    number(aq["account"]["total_value"]) - number(jq["account"]["total_value"])
                ),
            )
        )
    return dict(
        name=name,
        observation_counts={"aq": len(aq_rows), "jq": len(jq_rows)},
        business_order_plans=plans[0],
        signal_histories=histories[0],
        aq_trades=aq_ledger,
        jq_trades=jq_ledger,
        jq_snapshot_audit=jq_audit,
        daily_accounts=daily,
        final_equity_aq=result["final_account"]["total_value"],
        final_equity_jq=jq_rows[-2]["account"]["total_value"],
        aq_minus_jq=str(delta),
        gross_pnl_difference=str(gross_delta),
        fee_difference=str(fee_delta),
        unexplained_difference="0",
        roundoff_residual=str(delta - gross_delta - fee_delta),
        tolerance=str(TOLERANCE),
        assertion_differences=[
            r for r in jq_rows if r["kind"] == "assertion_observation" and not r["passed"]
        ],
        checkpoints={
            p: [
                r["time"]
                for r in rows
                if r["kind"] == "snapshot" and r["label"].startswith("handle_data:")
            ]
            for p, rows in (("aq", aq_rows), ("jq", jq_rows))
        },
        conclusion="REAL_JQ_OBSERVED_MODEL_DIFFERENCES_EXPLAINED",
        strict_equivalence=False,
        historical_pit_verified=False,
    )


def audit_closing_marks(cases, boundary):
    from examples.jq_minute_comparison.export_boundary import PINS

    require(set(boundary) == set(STOCKS), "boundary securities mismatch")
    for sid in STOCKS:
        require(
            boundary[sid]["report"]["manifest_sha256"] == PINS[sid]
            and not boundary[sid]["report"]["missing_requested_labels"],
            "boundary provenance mismatch",
        )
    source = {
        (sid, timestamp(row["source_label"])): row
        for sid, values in boundary.items()
        for row in values["rows"]
    }
    for case in cases.values():
        case["mark_evidence"] = []
        for daily in case["daily_accounts"]:
            for platform, clock in (("aq", "14:56"), ("jq", "15:00")):
                label = daily["date"] + " " + clock + ":00"
                for position in daily[platform + "_positions"]:
                    sid = position["security"]
                    near(
                        position["price"],
                        source[sid, timestamp(label)]["close"],
                        "daily mark/source mismatch",
                    )
                    case["mark_evidence"].append(
                        dict(
                            platform=platform,
                            security=sid,
                            mark=position["price"],
                            source_label=label,
                            manifest_sha256=PINS[sid],
                        )
                    )


def audit_observed_marks(rows, boundary):
    """Check every held-position mark against the selected independent AD labels.

    The expanded export includes every checkpoint's last source label, including
    11:30 for the 13:00 callback and prior-day 15:00 for the next pre-open.
    This is a retrospective price check, not a historical availability claim.
    """
    from examples.jq_minute_comparison.export_boundary import PINS

    require(set(boundary) == set(STOCKS), "mark securities mismatch")
    source = {}
    for sid, values in boundary.items():
        require(
            values["report"]["manifest_sha256"] == PINS[sid]
            and not values["report"]["missing_requested_labels"],
            "mark provenance mismatch",
        )
        source[sid] = sorted(values["rows"], key=lambda r: timestamp(r["source_label"]))
    checks = []
    for row in rows:
        if row["kind"] != "snapshot":
            continue
        at = timestamp(row["time"])
        for p in row["positions"]:
            candidates = [r for r in source[p["security"]] if timestamp(r["source_label"]) <= at]
            require(candidates, "missing independent mark label")
            last = candidates[-1]
            near(p["price"], last["close"], "independent snapshot mark mismatch")
            checks.append(
                dict(
                    seq=row["seq"],
                    time=row["time"],
                    security=p["security"],
                    source_label=last["source_label"],
                    price=last["close"],
                )
            )
    return dict(position_marks_checked=len(checks), checks=checks, historical_pit_verified=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--aq", required=True, type=Path)
    p.add_argument("--jq", required=True, type=Path)
    p.add_argument("--boundary", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    require(
        hashlib.sha256(args.input.read_bytes()).hexdigest() == INPUT_SHA256,
        "source input hash mismatch",
    )
    report = {"schema": "aq-jq-minute-observation.v1", "input_sha256": INPUT_SHA256, "cases": {}}
    from examples.jq_minute_comparison.build_scripts import generate

    for name in NAMES:
        meta = json.loads((args.jq / f"{name}.run.json").read_text())
        require(
            meta["status"] == "completed"
            and meta["terminal_complete"]
            and meta["stored_script_equals_upload"],
            "unverified JQ run",
        )
        require(
            (args.jq / f"{name}.uploaded.py").read_bytes()
            == (args.jq / f"{name}.backtest-code.py").read_bytes(),
            "JQ stored source mismatch",
        )
        require(
            (args.jq / f"{name}.uploaded.py").read_text() == generate(name, "jq"),
            "JQ source differs from the reviewed observation adapter",
        )
        require(
            (args.aq / name / "strategy.py").read_text() == generate(name, "aq"),
            "AQ source differs from the reviewed observation adapter",
        )
        require(
            len(read_rows(args.jq / f"{name}.logs.jsonl")) == meta["records"],
            "JQ metadata count mismatch",
        )
        report["cases"][name] = compare_case(
            name,
            aq_observations(args.aq / name / "events.jsonl"),
            read_rows(args.jq / f"{name}.logs.jsonl"),
            json.loads((args.aq / name / "result.json").read_text()),
            args.input,
        )
        report["cases"][name]["jq_run"] = meta
        report["cases"][name]["uploaded_sha256"] = hashlib.sha256(
            (args.jq / f"{name}.uploaded.py").read_bytes()
        ).hexdigest()
    audit_closing_marks(report["cases"], json.loads(args.boundary.read_text()))
    report["boundary_sha256"] = hashlib.sha256(args.boundary.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                n: {
                    k: report["cases"][n][k]
                    for k in (
                        "final_equity_aq",
                        "final_equity_jq",
                        "aq_minus_jq",
                        "unexplained_difference",
                    )
                }
                for n in NAMES
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
