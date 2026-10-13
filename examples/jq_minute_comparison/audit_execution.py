"""Read-only Decimal reconciliation of minute artifacts, including strict capacity.

This checks the native event stream; it never supplies orders, fills or account
values to the engine. Legacy overruns may be reported only for before/after audits.
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from examples.jq_minute_comparison.compare import near, number, read_rows, require, timestamp


def audit_run(root, *, enforce_strict_cap=True):
    root = Path(root)
    result = json.loads((root / "result.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    complete = json.loads((root / "complete.json").read_text())
    require(
        complete["identity"] == result["identity"] == manifest["identity"], "run identity mismatch"
    )
    for name, digest in complete["artifacts"].items():
        require(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == digest,
            "artifact changed: " + name,
        )
    for filename, key in (("input.json", "input_sha256"), ("strategy.py", "strategy_sha256")):
        require(
            hashlib.sha256((root / filename).read_bytes()).hexdigest() == manifest[key],
            "frozen source hash mismatch: " + filename,
        )
    doc = json.loads((root / "input.json").read_text())
    cfg = manifest["config"]
    mode = cfg.get("matching", "next_open")
    require(mode in {"next_open", "jq_current"}, "unknown matching model")
    require(not result["is_test_double"], "native ledger required")
    require(
        result["native_classes"]["account"] == "rqalpha.portfolio.account.Account",
        "native account required",
    )
    rows, by_day = {}, defaultdict(list)
    for raw in doc["minute"]:
        row = {
            **raw,
            "start": timestamp(raw.get("bar_start") or raw["replay_bar_start"]),
            "end": timestamp(raw.get("bar_end") or raw["replay_bar_end"]),
            "visible": timestamp(raw.get("available_at") or raw["replay_bar_end"]),
        }
        rows[row["security"], row["end"]] = row
        by_day[row["security"], str(row["end"].date())].append(row)
    for values in by_day.values():
        values.sort(key=lambda r: r["end"])
    states = {(s["security"], s["date"]): s for s in doc["states"]}
    orders = {o["order_id"]: o for o in result["orders"]}
    cash = number(cfg["initial_cash"])
    qty, basis, bought_today, consumed = [defaultdict(int) for _ in range(4)]
    order_value, order_fee = defaultdict(Decimal), defaultdict(Decimal)
    order_states, filled, checks, day = {}, defaultdict(int), 0, None
    ledger, budgets, violations, event_trades = [], {}, [], []
    for e in read_rows(root / "events.jsonl"):
        now = timestamp(e["at"])
        date = str(now.date())
        if date != day:
            day = date
            bought_today.clear()
        if e["kind"] == "order_event":
            order_states[e["order_id"]] = e
        elif e["kind"] == "trade":
            event_trades.append(e)
            sid, oid, amount = e["security"], e["order_id"], e["amount"]
            o, bar = orders[oid], rows[sid, now]
            submitted = timestamp(o["add_time"])
            require(bar["visible"] <= now and bar["end"] <= now, "fill before bar is visible")
            require(amount > 0 and amount % 100 == 0, "invalid stock fill quantity")
            if mode == "next_open":
                require(
                    now > submitted and bar["start"] >= submitted,
                    "next-open order used earlier interval",
                )
                near(e["price"], bar["open"], "next-open fill price mismatch")
            elif now == submitted:
                near(e["price"], bar["close"], "immediate fill differs from visible close")
            else:
                require(
                    now > submitted and bar["start"] >= submitted and o["price"] > 0,
                    "invalid resting-order interval",
                )
                crossed = (
                    number(o["price"]) > number(bar["low"])
                    if e["side"] == "BUY"
                    else number(o["price"]) < number(bar["high"])
                )
                require(crossed, "resting order did not strictly cross")
                near(e["price"], o["price"], "resting fill not at limit")
            if o["price"]:
                require(
                    (e["price"] <= o["price"])
                    if e["side"] == "BUY"
                    else (e["price"] >= o["price"]),
                    "fill violated order limit",
                )
            key = (sid, e["at"])
            consumed[key] += amount
            raw_cap = Fraction(str(bar["volume"])) * Fraction(str(cfg["volume_percent"]))
            cap = int(raw_cap // 100) * 100
            budgets[key] = dict(
                security=sid,
                at=e["at"],
                source_volume=bar["volume"],
                ratio=cfg["volume_percent"],
                strict_cap=cap,
                filled=consumed[key],
            )
            if consumed[key] > cap:
                violations.append(
                    {
                        **budgets[key],
                        "excess_over_raw_budget": str(Fraction(consumed[key]) - raw_cap),
                    }
                )
                require(not enforce_strict_cap, "exact minute participation capacity exceeded")
            value = number(amount) * number(e["price"])
            order_value[oid] += value
            due = max(
                number(cfg["min_commission"]), order_value[oid] * number(cfg["commission_rate"])
            )
            fee = max(Decimal(0), due - order_fee[oid])
            order_fee[oid] += fee
            tax = value * number(cfg["sell_tax"]) if e["side"] == "SELL" else Decimal(0)
            near(e["commission"], fee, "cumulative native commission mismatch")
            near(e["tax"], tax, "native sell tax mismatch")
            filled[oid] += amount
            if e["side"] == "BUY":
                cash -= value
                qty[sid] += amount
                basis[sid] += value
                bought_today[sid] += amount
            else:
                require(
                    e["side"] == "SELL" and amount <= qty[sid] - bought_today[sid],
                    "T+1 inventory violation",
                )
                basis[sid] -= basis[sid] * amount / qty[sid]
                qty[sid] -= amount
                cash += value
            cash -= fee + tax
            require(cash >= 0, "negative cash after trade")
            ledger.append(
                dict(
                    at=e["at"],
                    security=sid,
                    order_id=oid,
                    amount=amount,
                    side=e["side"],
                    price=e["price"],
                    commission=str(fee),
                    tax=str(tax),
                    cash_after=str(cash),
                )
            )
        elif e["kind"] == "account":
            near(
                number(e["available_cash"]) + number(e["locked_cash"]), cash, "event cash mismatch"
            )
            require(
                {s: q for s, q in qty.items() if q}
                == {s: p["total_amount"] for s, p in e["positions"].items()},
                "event holdings mismatch",
            )
            market_value = Decimal(0)
            for sid, p in e["positions"].items():
                pending_sell = sum(
                    o["amount"] - max(o["filled"], filled[o["order_id"]])
                    for o in order_states.values()
                    if o["security"] == sid and not o["is_buy"] and o["status"] in {"new", "held"}
                )
                require(
                    p["closeable_amount"] == qty[sid] - bought_today[sid] - pending_sell,
                    "closeable inventory mismatch",
                )
                near(p["avg_cost"], basis[sid] / qty[sid], "holding cost mismatch")
                visible = [r for r in by_day[sid, date] if r["end"] <= now and r["visible"] <= now]
                mark = number(
                    visible[-1]["close"] if visible else states[sid, date]["previous_close"]
                )
                near(p["price"], mark, "event price clock mismatch")
                near(p["value"], mark * qty[sid], "event position value mismatch")
                market_value += mark * qty[sid]
            near(e["positions_value"], market_value, "event position value sum mismatch")
            near(e["total_value"], cash + market_value, "event equity mismatch")
            checks += 1
    require(len(ledger) == len(result["trades"]), "result trade stream incomplete")
    for actual, expected in zip(event_trades, result["trades"], strict=True):
        require(
            {k: actual[k] for k in expected} == expected, "result trade differs from native event"
        )
    near(
        result["final_account"]["available_cash"] + result["final_account"]["locked_cash"],
        cash,
        "final cash mismatch",
    )
    return dict(
        native_account_events=checks,
        exact_cash=str(cash),
        ledger=ledger,
        capacity_checks=list(budgets.values()),
        capacity_violations=violations,
        policies={"matching": mode, "cash_policy": cfg.get("cash_policy", "reject")},
        all_costs_and_T1_checked=True,
        float_tolerance="1e-7",
    )
