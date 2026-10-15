"""Read-only accounting and causal checks. Never drives execution or repairs a book."""

import csv
import json
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, DecimalException, localcontext
from pathlib import Path

from .inputs import digest

TOLERANCE = Decimal("0.000001")


def audit(result, data, manifest):
    with localcontext(prec=50):
        return _audit(result, data, manifest)


def _audit(result, data, manifest):
    errors = []

    def check(condition, code, **details):
        if not condition:
            errors.append({"code": code, **details})

    def equal(a, b, code, **details):
        try:
            left, right = Decimal(str(a)), Decimal(str(b))
            matches = left.is_finite() and right.is_finite() and abs(left - right) <= TOLERANCE
        except (DecimalException, ValueError, TypeError):
            matches = False
        check(
            matches,
            code,
            actual=str(a),
            expected=str(b),
            **details,
        )

    check([a["date"] for a in result["accounts"]] == data.days, "DAILY_ACCOUNT_COVERAGE")
    check([d["date"] for d in result["decisions"]] == data.days, "DAILY_DECISION_COVERAGE")
    check(len({t["id"] for t in result["trades"]}) == len(result["trades"]), "DUPLICATE_TRADE")
    check(len({o["id"] for o in result["orders"]}) == len(result["orders"]), "DUPLICATE_ORDER")
    check(result["native_account_owner"] == "rqalpha.portfolio.account", "NATIVE_LEDGER_OWNER")
    orders = {o["id"]: o for o in result["orders"]}
    cash = Decimal(manifest["execution"]["initial_cash"])
    quantities = {s: manifest["execution"].get("initial_positions", {}).get(s, 0) for s in data.ids}
    fee_total, tax_total = Decimal(0), Decimal(0)
    receivables = []
    dividend_total = Decimal(0)
    formal = data.corporate_actions is not None
    corporate_basis, stage_dates = {}, {}
    if formal:
        check(
            [r["date"] for r in result.get("corporate_daily_pnl", [])] == data.days,
            "CORPORATE_PNL_COVERAGE",
        )
        check(
            result.get("corporate_schedule_sha256") == digest(data.document["corporate_schedule"]),
            "CORPORATE_SOURCE_BOUND",
        )
        check(
            result.get("corporate_account_model") == data.document["corporate_account_model"],
            "CORPORATE_MODEL_BOUND",
        )
    previous_equity = cash + sum(
        quantities[s] * Decimal(str(data.row(s, data.previous[data.days[0]])["close"]))
        for s in data.ids
    )
    native_pnl = {r["date"]: r["pnl"] for r in result.get("corporate_daily_pnl", [])}
    for day, account, decision in zip(
        data.days, result["accounts"], result["decisions"], strict=True
    ):
        check(
            decision["cutoff"] == data.previous[day] + "T15:00:00+08:00", "SIGNAL_CUTOFF", date=day
        )
        check(
            decision["history_sha256"] == digest(data.history(day)),
            "SIGNAL_HISTORY_BOUND",
            date=day,
        )
        # Read-only source-action reconciliation, independent of native callbacks.
        for action in data.actions:
            if action["ex_date"] == day and action["kind"] == "cash_dividend":
                amount = quantities[action["security"]] * Decimal(str(action["cash_per_share"]))
                receivables.append((action["pay_date"], amount))
                dividend_total += amount
        for action in data.actions:
            if action["ex_date"] == day and action["kind"] == "split":
                sid = action["security"]
                quantities[sid] = int(
                    (Decimal(quantities[sid]) * Decimal(str(action["ratio"]))).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )
        cash += sum(amount for pay, amount in receivables if pay <= day)
        receivables = [(pay, amount) for pay, amount in receivables if pay > day]
        locked = {s: 0 for s in data.ids}
        pending = {s: 0 for s in data.ids}
        corporate_receivable = Decimal(0)
        for event in data.corporate_actions or []:
            key, sid = event["entitlement_key"], event["security"]
            if key not in corporate_basis:
                continue
            basis = corporate_basis[key]
            amount = basis * Decimal(event["cash_per_share"])
            new = basis * (
                Decimal(event["bonus_share_ratio"]) + Decimal(event["capitalization_share_ratio"])
            )
            if event["ex_date"] == day:
                dividend_total += amount
                stage_dates[key][key + ":ex"] = day
            if amount and event["ex_date"] <= day < event["payment_date"]:
                corporate_receivable += amount
            if Decimal(event["cash_per_share"]) and event["payment_date"] == day:
                cash += amount
                stage_dates[key][event["settlement_keys"]["cash"]] = day
            if Decimal(event["bonus_share_ratio"]) + Decimal(event["capitalization_share_ratio"]):
                if event["ex_date"] <= day < event["share_credit_date"]:
                    pending[sid] += int(new)
                if event["share_credit_date"] == day:
                    quantities[sid] += int(new)
                    stage_dates[key][event["settlement_keys"]["stock_credit"]] = day
                if event["share_credit_date"] <= day < event["new_shares_tradable_date"]:
                    locked[sid] += int(new)
                if event["new_shares_tradable_date"] == day:
                    stage_dates[key][event["settlement_keys"]["stock_tradable"]] = day
        start_quantities = quantities.copy()
        for sid in data.ids:
            start_quantities[sid] -= locked[sid]
        trades = [t for t in result["trades"] if t["date"] == day]
        traded_by_security = {}
        for trade in trades:
            sid, q, price = trade["security"], trade["quantity"], Decimal(trade["price"])
            order = orders.get(trade["order_id"])
            traded_by_security[sid] = traded_by_security.get(sid, 0) + q
            ratio = manifest["execution"].get("volume_ratio")
            if ratio is not None:
                cap = (
                    int(
                        (
                            Decimal(str(data.row(sid, day)["volume"])) * Decimal(str(ratio)) / 100
                        ).to_integral_value(ROUND_FLOOR)
                    )
                    * 100
                )
                check(
                    traded_by_security[sid] <= cap,
                    "STRICT_VOLUME_CAP",
                    date=day,
                    security=sid,
                    filled=traded_by_security[sid],
                    cap=cap,
                )
            check(
                order is not None and order["security"] == sid and order["side"] == trade["side"],
                "TRADE_ORDER_LINK",
                trade=trade["id"],
            )
            check(not data.row(sid, day)["suspended"], "NO_SUSPENDED_FILL", date=day, security=sid)
            equal(price, data.row(sid, day)["open"], "OBSERVED_OPEN_PRICE", date=day, security=sid)
            check(trade["at"] == day + " 09:30:00", "TRADE_CLOCK", trade=trade["id"])
            commission = max(price * q * Decimal("0.0003"), Decimal(5))
            tax = price * q * Decimal("0.001") if trade["side"] == "SELL" else Decimal(0)
            equal(trade["commission"], commission, "COMMISSION", trade=trade["id"])
            equal(trade["tax"], tax, "SELL_TAX", trade=trade["id"])
            fee_total += Decimal(trade["commission"])
            tax_total += Decimal(trade["tax"])
            if trade["side"] == "BUY":
                check(q > 0 and q % 100 == 0, "BUY_BOARD_LOT", trade=trade["id"])
                cash -= price * q + commission + tax
                quantities[sid] += q
            else:
                check(0 < q <= start_quantities[sid], "T_PLUS_ONE", trade=trade["id"])
                cash += price * q - commission - tax
                quantities[sid] -= q
                start_quantities[sid] -= q
            check(cash >= -TOLERANCE, "SHARED_CASH_NONNEGATIVE", trade=trade["id"])
        for event in data.corporate_actions or []:
            if event["record_date"] == day:
                key = event["entitlement_key"]
                corporate_basis[key] = quantities[event["security"]]
                stage_dates[key] = {event["settlement_keys"]["record"]: day}
        equal(account["cash"], cash, "CASH_FROM_FILLS_AND_ACTIONS", date=day)
        actual = {s: account["positions"].get(s, {}).get("quantity", 0) for s in data.ids}
        check(actual == quantities, "POSITIONS_FROM_FILLS_AND_ACTIONS", date=day)
        value, receivable, pending_value = Decimal(0), Decimal(0), Decimal(0)
        if formal:
            for sid in data.ids:
                pos = account["positions"].get(sid, {})
                check(
                    pos.get("restricted_shares", 0) == locked[sid],
                    "CORPORATE_RESTRICTED_SHARES",
                    date=day,
                    security=sid,
                )
                check(
                    pos.get("pending_shares", 0) == pending[sid],
                    "CORPORATE_PENDING_SHARES",
                    date=day,
                    security=sid,
                )
                check(
                    pos.get("sellable", 0) == start_quantities[sid],
                    "CORPORATE_SELLABLE",
                    date=day,
                    security=sid,
                )
                expected = pending[sid] * Decimal(str(data.row(sid, day)["close"]))
                equal(
                    pos.get("pending_share_value", "0"),
                    expected,
                    "CORPORATE_PENDING_VALUE",
                    date=day,
                    security=sid,
                )
                pending_value += expected
        for sid, pos in account["positions"].items():
            equal(
                pos["price"], data.row(sid, day)["close"], "CLOSE_VALUATION", date=day, security=sid
            )
            equal(
                pos["value"],
                Decimal(pos["price"]) * pos["quantity"],
                "POSITION_VALUE",
                date=day,
                security=sid,
            )
            value += Decimal(pos["value"])
            receivable += Decimal(pos["dividend_receivable"])
        equal(
            receivable,
            sum(amount for pay, amount in receivables) + corporate_receivable,
            "SOURCE_ACTION_RECEIVABLE",
            date=day,
        )
        equal(
            account["equity"],
            Decimal(account["cash"]) + value + receivable + pending_value,
            "EQUITY_IDENTITY",
            date=day,
        )
        check(Decimal(account["cash"]) >= -TOLERANCE, "ACCOUNT_CASH_NONNEGATIVE", date=day)
        if formal:
            expected_equity = (
                cash
                + sum(quantities[s] * Decimal(str(data.row(s, day)["close"])) for s in data.ids)
                + sum(amount for pay, amount in receivables)
                + corporate_receivable
                + pending_value
            )
            equal(
                native_pnl.get(day, "NaN"),
                expected_equity - previous_equity,
                "NATIVE_DAILY_PNL_FROM_EQUITY_CHANGE",
                date=day,
            )
            previous_equity = expected_equity
        submissions = [s for s in result["submissions"] if s["date"] == day]
        signed_ids = [(0 if s["quantity"] < 0 else 1, s["security"]) for s in submissions]
        check(signed_ids == sorted(signed_ids), "SELL_BEFORE_BUY_SECURITY_ORDER", date=day)
        for sub in submissions:
            check(
                Decimal(sub["cash_before"]) >= -TOLERANCE
                and Decimal(sub["cash_after"]) >= -TOLERANCE,
                "SUBMISSION_CASH_NONNEGATIVE",
                date=day,
                security=sub["security"],
            )
            if sub["suspended"]:
                check(
                    sub["returned_none"],
                    "SUSPENSION_REJECTION_OBSERVED",
                    date=day,
                    security=sub["security"],
                )
    if formal:
        recorded = result.get("corporate_entitlements", {})
        for sid in data.ids:
            expected_keys = {
                e["entitlement_key"]
                for e in data.corporate_actions
                if e["security"] == sid and e["entitlement_key"] in corporate_basis
            }
            check(
                set(recorded.get(sid, {})) == expected_keys,
                "CORPORATE_JOURNAL_COVERAGE",
                security=sid,
            )
        for event in data.corporate_actions:
            key = event["entitlement_key"]
            if key not in corporate_basis:
                continue
            entry = recorded.get(event["security"], {}).get(key, {})
            check(
                entry.get("event") == event and entry.get("source_sha256") == digest(event),
                "CORPORATE_REVISION_BOUND",
                event=key,
            )
            check(
                entry.get("basis") == corporate_basis[key], "CORPORATE_REGISTERED_BASIS", event=key
            )
            check(entry.get("stages") == stage_dates[key], "CORPORATE_STAGE_JOURNAL", event=key)
            equal(
                entry.get("cash", "NaN"),
                corporate_basis[key] * Decimal(event["cash_per_share"]),
                "CORPORATE_REGISTERED_CASH",
                event=key,
            )
            check(
                entry.get("shares")
                == corporate_basis[key]
                * (
                    Decimal(event["bonus_share_ratio"])
                    + Decimal(event["capitalization_share_ratio"])
                ),
                "CORPORATE_REGISTERED_SHARES",
                event=key,
            )
    for order in result["orders"]:
        fills = [t for t in result["trades"] if t["order_id"] == order["id"]]
        check(
            sum(t["quantity"] for t in fills) == order["filled"],
            "ORDER_FILLED_SUM",
            order=order["id"],
        )
        check(
            order["status"] in {"FILLED", "CANCELLED", "REJECTED"},
            "NO_ACTIVE_TERMINAL_ORDER",
            order=order["id"],
        )
    contract_differences, contract_evidence_gaps = [], []
    if "session end" in manifest["execution"].get("unfilled", "").lower():
        for order in result["orders"]:
            if order["status"] != "CANCELLED" or order["filled"] >= order["quantity"]:
                continue
            observed = [
                e
                for e in result.get("order_events", [])
                if e["order"]["id"] == order["id"] and e["order"]["status"] == "CANCELLED"
            ]
            if not observed:
                contract_evidence_gaps.append(
                    {"code": "CANCELLATION_TIME_MISSING", "order": order["id"]}
                )
            elif observed[0]["phase"] == "EXECUTE":
                contract_differences.append(
                    {
                        "code": "CANCELLED_BEFORE_SESSION_END",
                        "order": order["id"],
                        "observed_at": observed[0]["at"],
                        "expected": manifest["execution"]["unfilled"],
                    }
                )
    return {
        "schema": "aq-daily-audit.v1",
        "passed": not errors and not contract_differences and not contract_evidence_gaps,
        "accounting_passed": not errors,
        "errors": errors,
        "contract_differences": contract_differences,
        "contract_evidence_gaps": contract_evidence_gaps,
        "economic_scope": (
            "price_only_model_book_not_total_return"
            if data.document.get("research_model") == "price_only_no_actions"
            else "cash_quantity_and_gross_action_replay"
        ),
        "economic_acceptance": data.document.get("economic_acceptance", "synthetic_only"),
        "gross_dividend_entitlement": str(dividend_total),
        "commission": str(fee_total),
        "tax": str(tax_total),
        "sessions": len(data.days),
        "trades": len(result["trades"]),
        "native_cash_tolerance_CNY": str(TOLERANCE),
        "real_market_data": data.document["real_market_data"],
        "jq_comparison": "not_collected",
    }


def write_report(root, result, report):
    with localcontext(prec=50):
        _write_report(root, result, report)


def _write_report(root, result, report):
    root = Path(root)
    (root / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (root / "equity.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        formal = "corporate_entitlements" in result
        writer.writerow(
            ["date", "cash", "market_value", "dividend_receivable", "equity", "holding_count"]
            + (["pending_share_value", "restricted_shares", "pending_shares"] if formal else [])
        )
        for row in result["accounts"]:
            positions = row["positions"].values()
            writer.writerow(
                [
                    row["date"],
                    row["cash"],
                    str(sum(Decimal(p["value"]) for p in positions)),
                    str(sum(Decimal(p["dividend_receivable"]) for p in positions)),
                    row["equity"],
                    sum(p["quantity"] > 0 for p in positions),
                ]
                + (
                    [
                        str(sum(Decimal(p["pending_share_value"]) for p in positions)),
                        sum(p["restricted_shares"] for p in positions),
                        sum(p["pending_shares"] for p in positions),
                    ]
                    if formal
                    else []
                )
            )
