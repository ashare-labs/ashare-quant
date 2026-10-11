"""Read-only accounting and causal checks. Never drives execution or repairs a book."""

import csv
import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
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
        check(
            abs(Decimal(str(a)) - Decimal(str(b))) <= TOLERANCE,
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
    quantities = {s: 0 for s in data.ids}
    fee_total, tax_total = Decimal(0), Decimal(0)
    receivables = []
    dividend_total = Decimal(0)
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
        start_quantities = quantities.copy()
        trades = [t for t in result["trades"] if t["date"] == day]
        for trade in trades:
            sid, q, price = trade["security"], trade["quantity"], Decimal(trade["price"])
            order = orders.get(trade["order_id"])
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
        equal(account["cash"], cash, "CASH_FROM_FILLS_AND_ACTIONS", date=day)
        actual = {s: account["positions"].get(s, {}).get("quantity", 0) for s in data.ids}
        check(actual == quantities, "POSITIONS_FROM_FILLS_AND_ACTIONS", date=day)
        value, receivable = Decimal(0), Decimal(0)
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
            sum(amount for pay, amount in receivables),
            "SOURCE_ACTION_RECEIVABLE",
            date=day,
        )
        equal(
            account["equity"],
            Decimal(account["cash"]) + value + receivable,
            "EQUITY_IDENTITY",
            date=day,
        )
        check(Decimal(account["cash"]) >= -TOLERANCE, "ACCOUNT_CASH_NONNEGATIVE", date=day)
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
    return {
        "schema": "aq-daily-audit.v1",
        "passed": not errors,
        "errors": errors,
        "economic_scope": "cash_quantity_and_gross_action_replay",
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
        writer.writerow(
            ["date", "cash", "market_value", "dividend_receivable", "equity", "holding_count"]
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
            )
