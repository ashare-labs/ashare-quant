"""Read-only Decimal reconciliation of minute artifacts, including strict capacity.

This checks the native event stream; it never supplies orders, fills or account
values to the engine. Legacy overruns may be reported only for before/after audits.
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from examples.jq_minute_comparison.compare import near, number, read_rows, require, timestamp


def audit_run(root, *, enforce_strict_cap=True):
    # Native imports may lower the ambient precision. The independent cash
    # reconciliation must retain cents without changing the caller's context.
    with localcontext() as context:
        context.prec = 50
        return _audit_run(root, enforce_strict_cap=enforce_strict_cap)


def _audit_run(root, *, enforce_strict_cap=True):
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
    jq_lifecycle = cfg.get("lifecycle") == "jq_minute"
    jq_capacity = cfg.get("capacity_policy") == "jq_daily_initial"
    require(mode in {"next_open", "jq_current"}, "unknown matching model")
    require(not result["is_test_double"], "native ledger required")
    require(
        result["native_classes"]["account"]
        == (
            "ashare_quant.second_batch.reservation.BudgetAccount"
            if jq_capacity
            else "rqalpha.portfolio.account.Account"
        ),
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
    observations = doc.get("session_observations", {})
    opening = {(r["security"], r["date"]): r for r in observations.get("opening_prices", [])}
    closing = {(r["security"], r["date"]): r for r in observations.get("closing_marks", [])}
    daily = {(r["security"], r["date"]): r for r in observations.get("daily", [])}
    orders = {o["order_id"]: o for o in result["orders"]}
    cash = number(cfg["initial_cash"])
    qty, basis, bought_today, consumed = [defaultdict(int) for _ in range(4)]
    order_value, order_fee, order_tax = (
        defaultdict(Decimal),
        defaultdict(Decimal),
        defaultdict(Decimal),
    )
    origin_cost = {}
    order_states, filled, checks, day = {}, defaultdict(int), 0, None
    initial_reserve = {}
    ledger, budgets, violations, event_trades = [], {}, [], []
    observer_checks, first_observations, observed_trade_ids = [], [], set()
    pending = native_request = native_return = None
    aliases = {"order": "order_shares", "order_target": "order_to"}

    def visible_price(sid, now):
        visible = [
            r for r in by_day[sid, str(now.date())] if r["end"] <= now and r["visible"] <= now
        ]
        if visible:
            return number(visible[-1]["close"])
        quote = opening.get((sid, str(now.date())))
        if quote and timestamp(quote.get("available_at") or quote["replay_available_at"]) <= now:
            return number(quote["price"])
        return number(states[sid, str(now.date())]["previous_close"])

    def remaining_reserve(oid, order):
        remaining = order["amount"] - max(order["filled"], filled[oid])
        if jq_capacity and order["is_buy"] and order["price"]:
            return (
                max(Decimal(0), initial_reserve[oid] - order_value[oid] - order_fee[oid])
                if remaining
                else Decimal(0)
            )
        return initial_reserve[oid] * remaining / order["amount"]

    def executable_price(sid, now, bar):
        # Do not reuse the valuation fallback: a previous close or an opening
        # scalar at 13:00 is not evidence of an executable boundary price.
        if bar is not None:
            require(number(bar["volume"]) > 0, "fill at zero-volume minute")
            return number(bar["close"])
        if now.strftime("%H:%M") == "09:30":
            quote = opening.get((sid, str(now.date())))
            require(
                quote is not None
                and quote["purpose"] == "opening_price"
                and quote["price_basis"] == "raw"
                and timestamp(quote.get("available_at") or quote["replay_available_at"]) <= now,
                "fill without visible opening execution price",
            )
            return number(quote["price"])
        require(now.strftime("%H:%M") == "13:00", "invalid no-bar execution clock")
        visible = [
            r for r in by_day[sid, str(now.date())] if r["end"] <= now and r["visible"] <= now
        ]
        require(visible, "fill without visible same-day resume price")
        return number(visible[-1]["close"])

    def reservation():
        # Native Account reserves the initial request's notional/estimated fees,
        # then releases that budget proportionally to each fill. Rebuild it from
        # creation and trade events; observer totals are never the authority.
        return sum(
            (
                remaining_reserve(oid, o)
                for oid, o in order_states.items()
                if o["status"] in {"new", "held"}
            ),
            Decimal(0),
        )

    def check_account(account, held, now):
        reserved = reservation()
        near(account["locked_cash"], reserved, "event frozen cash mismatch")
        near(account["available_cash"], cash - reserved, "event available cash mismatch")
        near(
            number(account["available_cash"]) + number(account["locked_cash"]),
            cash,
            "event cash mismatch",
        )
        require(
            {s: q for s, q in qty.items() if q} == {s: p["total_amount"] for s, p in held.items()},
            "event holdings mismatch",
        )
        market_value = Decimal(0)
        for sid, p in held.items():
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
            if "today_amount" in p and not isinstance(p["today_amount"], dict):
                require(p["today_amount"] == bought_today[sid], "today inventory mismatch")
            mark = visible_price(sid, now)
            quote = closing.get((sid, str(now.date())))
            if (
                quote
                and timestamp(quote.get("available_at") or quote["replay_available_at"]) <= now
            ):
                mark = number(quote["price"])
            near(p["price"], mark, "event price clock mismatch")
            near(p["value"], mark * qty[sid], "event position value mismatch")
            market_value += mark * qty[sid]
        near(account["positions_value"], market_value, "event position value sum mismatch")
        near(account["total_value"], cash + market_value, "event equity mismatch")

    for e in read_rows(root / "events.jsonl"):
        now = timestamp(e["at"])
        date = str(now.date())
        if date != day:
            day = date
            bought_today.clear()
        if e["kind"] == "order_event":
            order_states[e["order_id"]] = e
            if e["order_id"] not in initial_reserve:
                origin_cost[e["order_id"]] = (
                    basis[e["security"]] / qty[e["security"]] if qty[e["security"]] else Decimal(0)
                )
                price = number(e["price"]) or visible_price(e["security"], now)
                gross = price * e["amount"]
                fee = max(number(cfg["min_commission"]), gross * number(cfg["commission_rate"]))
                initial_reserve[e["order_id"]] = (
                    gross
                    if jq_capacity and e["is_buy"] and e["price"]
                    else (gross + fee if e["is_buy"] else fee + gross * number(cfg["sell_tax"]))
                )
        elif e["kind"] == "trade":
            event_trades.append(e)
            sid, oid, amount = e["security"], e["order_id"], e["amount"]
            o, bar = orders[oid], rows.get((sid, now))
            submitted = timestamp(o["add_time"])
            require(
                (bar is not None and bar["visible"] <= now and bar["end"] <= now)
                or (
                    jq_capacity and now == submitted and now.strftime("%H:%M") in {"09:30", "13:00"}
                ),
                "fill before bar is visible",
            )
            require(
                type(amount) is int and amount > 0 and (jq_capacity or amount % 100 == 0),
                "invalid stock fill quantity",
            )
            if mode == "next_open":
                require(
                    now > submitted and bar["start"] >= submitted,
                    "next-open order used earlier interval",
                )
                near(e["price"], bar["open"], "next-open fill price mismatch")
            elif now == submitted:
                price = executable_price(sid, now, bar) if jq_capacity else visible_price(sid, now)
                near(e["price"], price, "immediate fill differs from eligible visible price")
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
            key = (sid, e["at"], oid) if jq_capacity else (sid, e["at"])
            consumed[key] += amount
            volume = (
                daily[sid, date]["volume"] if jq_capacity and submitted == now else bar["volume"]
            )
            raw_cap = Fraction(str(volume)) * Fraction(str(cfg["volume_percent"]))
            cap = int(raw_cap) if jq_capacity and submitted != now else int(raw_cap // 100) * 100
            budgets[key] = dict(
                security=sid,
                at=e["at"],
                source_volume=volume,
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
                require(not enforce_strict_cap, "declared participation capacity exceeded")
            value = number(amount) * number(e["price"])
            order_value[oid] += value
            due = max(
                number(cfg["min_commission"]), order_value[oid] * number(cfg["commission_rate"])
            )
            fee = max(Decimal(0), due - order_fee[oid])
            order_fee[oid] += fee
            tax = value * number(cfg["sell_tax"]) if e["side"] == "SELL" else Decimal(0)
            order_tax[oid] += tax
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
        elif e["kind"] == "order_request":
            native_request = e
        elif e["kind"] == "order_return":
            native_return = e
        elif e["kind"] == "account":
            check_account(e, e["positions"], now)
            checks += 1
        elif e["kind"] == "log" and e["message"].startswith(("AQMIN_JSON ", "AQGAP_JSON ")):
            gap = e["message"].startswith("AQGAP_JSON ")
            row = json.loads(e["message"].split(" ", 1)[1])
            require(timestamp(row["time"]) == now, "AQ observer/native event clock mismatch")
            if gap and row["kind"] == "submit":
                row = {**row, "api": "order", "value": row["amount"]}
            if gap and row["kind"] == "return":
                require(
                    pending is not None and row["label"] == pending["label"],
                    "AQ gap return binding",
                )
                row = {
                    **row,
                    "kind": "order_return",
                    "api": "order",
                    "security": pending["security"],
                    "returned_none": row["order"] is None,
                }
            if row["kind"] == "submit":
                require(pending is None, "AQ missing observer return")
                pending, native_request, native_return = row, None, None
            elif row["kind"] == "order_return":
                require(
                    pending is not None
                    and native_request is not None
                    and native_return is not None,
                    "AQ observer return without native call",
                )
                require(
                    all(row[k] == pending[k] for k in ("time", "api", "security")),
                    "AQ observer return differs from submit",
                )
                require(
                    native_request["api"] == aliases.get(row["api"], row["api"])
                    and native_request["security"] == row["security"]
                    and native_request["value"] == pending["value"],
                    "AQ observer/native request mismatch",
                )
                require(
                    timestamp(native_request["at"]) == timestamp(native_return["at"]) == now,
                    "AQ native/observer return clock mismatch",
                )
                order = row["order"]
                require(row["returned_none"] is (order is None), "AQ inconsistent return flag")
                oid = None if order is None else str(order["order_id"])
                require(
                    oid == native_return["order_id"], "AQ observer/native returned order differs"
                )
                if order is not None:
                    expected = orders[oid]
                    require(
                        timestamp(order["add_time"]) == timestamp(expected["add_time"]) == now,
                        "AQ returned order time differs from submission",
                    )
                    require(
                        all(
                            order[k] == expected[k]
                            for k in (
                                ("security", "amount", "is_buy")
                                if jq_lifecycle
                                else ("security", "amount", "is_buy", "price")
                            )
                        ),
                        "AQ returned order identity/quantity differs",
                    )
                    require(
                        order["filled"] == filled[oid],
                        "AQ return fill amount differs from native trades",
                    )
                    if jq_lifecycle:
                        price = (
                            round(float(order_value[oid] / filled[oid]), 3) if filled[oid] else 0
                        )
                        near(order["price"], price, "AQ JQ order price projection mismatch")
                        near(
                            order["avg_cost"],
                            price if order["is_buy"] else origin_cost[oid],
                            "AQ JQ order cost projection mismatch",
                        )
                        near(
                            order["commission"],
                            order_fee[oid] + order_tax[oid],
                            "AQ JQ order commission projection mismatch",
                        )
                pending = None
            if row["kind"] not in {"snapshot", "account"}:
                continue
            held = {p["security"]: p for p in row["positions"]}
            require(len(held) == len(row["positions"]), "AQ duplicate observer position")
            check_account(row["account"], held, now)
            observer_checks.append(
                dict(
                    seq=row["seq"],
                    time=row["time"],
                    label=row["label"],
                    total_cash=str(cash),
                    available_cash=str(cash - reservation()),
                    locked_cash=str(reservation()),
                    no_trade_collection=row["trades"] is None,
                )
            )
            for order in row["orders"] or []:
                oid = str(order["order_id"])
                require(oid in order_states, "AQ observed order before native creation")
                require(
                    timestamp(order["add_time"]) == timestamp(orders[oid]["add_time"]) <= now,
                    "AQ snapshot order time mismatch",
                )
            if row["trades"] is not None:
                native_trades = {t["trade_id"]: t for t in event_trades}
                if jq_lifecycle:
                    native_trades = {i: t for i, t in native_trades.items() if t["at"][:10] == date}
                    if (
                        result["execution_policy"].get("terminal_day_queries") == "empty"
                        and now.strftime("%H:%M:%S.%f") == "23:59:59.001000"
                    ):
                        require(not row["orders"], "AQ terminal order collection not empty")
                        native_trades = {}
                seen = set()
                for trade in row["trades"]:
                    tid = str(trade["trade_id"])
                    require(
                        tid not in seen and tid in native_trades,
                        "AQ duplicate or future observed trade",
                    )
                    expected = native_trades[tid]
                    require(
                        timestamp(trade["time"] if gap else trade["at"])
                        == timestamp(expected["at"])
                        <= now,
                        "AQ trade follows first observation",
                    )
                    require(
                        all(
                            trade[k] == expected[k]
                            for k in (
                                ("order_id", "security", "amount")
                                if gap
                                else ("order_id", "security", "amount", "side")
                            )
                        ),
                        "AQ observed trade identity/quantity differs",
                    )
                    for key in ("price",) if gap else ("price", "commission", "tax"):
                        near(trade[key], expected[key], "AQ observed trade " + key + " mismatch")
                    if tid not in observed_trade_ids:
                        first_observations.append(
                            dict(
                                trade_id=tid,
                                fill_at=expected["at"],
                                observed_at=row["time"],
                                seq=row["seq"],
                            )
                        )
                        observed_trade_ids.add(tid)
                    seen.add(tid)
                require(seen == set(native_trades), "AQ observed trade collection incomplete")
    require(pending is None, "AQ missing observer order return")
    require(len(ledger) == len(result["trades"]), "result trade stream incomplete")
    for actual, expected in zip(event_trades, result["trades"], strict=True):
        require(
            {k: actual[k] for k in expected} == expected, "result trade differs from native event"
        )
    near(result["final_account"]["locked_cash"], reservation(), "final frozen cash mismatch")
    near(
        result["final_account"]["available_cash"],
        cash - reservation(),
        "final available cash mismatch",
    )
    near(
        result["final_account"]["available_cash"] + result["final_account"]["locked_cash"],
        cash,
        "final cash mismatch",
    )
    return dict(
        native_account_events=checks,
        observer_snapshots_checked=len(observer_checks),
        observer_checkpoints=observer_checks,
        first_trade_observations=first_observations,
        exact_cash=str(cash),
        ledger=ledger,
        capacity_checks=list(budgets.values()),
        capacity_violations=violations,
        policies={"matching": mode, "cash_policy": cfg.get("cash_policy", "reject")},
        all_costs_and_T1_checked=True,
        both_cash_components_checked=True,
        float_tolerance="1e-7",
    )
