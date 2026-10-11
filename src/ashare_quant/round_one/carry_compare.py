"""Read-only carry comparison. No comparison output is an execution input."""

import json
import re
from decimal import Decimal
from pathlib import Path

from .compare import attribute, clock, compare_d1, decimal, fields, read_json
from .market_compare import compare_markets

IDS = {"B14", "B16", "B17", "B18", "B22", "B33"}


def sell_cost_evidence(left, right, history, index):
    """Recognize the observed cost-vs-fill field distinction, not a generic rule."""
    jo, ao = left["orders"][index], right["orders"][index]
    if jo.get("is_buy") is not False or jo.get("action") != "close" or ao.get("side") != "SELL":
        return None
    positions = [
        (event["seq"], p)
        for event in history
        if event["kind"] == "snapshot" and event["seq"] < left["seq"]
        for p in event["positions"]
        if p["security"] == jo["security"] and p["total_amount"] > 0
    ]
    if not positions:
        return None
    seq, position = positions[-1]
    jq_trades = [t for t in left["trades"] if t["order_id"] == jo["order_id"]]
    aq_trades = [t for t in right["trades"] if t["order_id"] == ao["order_id"]]
    if not jq_trades or not aq_trades:
        return None
    jqty, aqty = sum(t["amount"] for t in jq_trades), sum(t["quantity"] for t in aq_trades)
    if jqty != jo["filled"] or aqty != ao["filled"] or jqty != aqty or jqty <= 0:
        return None
    jp = sum(Decimal(str(t["price"])) * t["amount"] for t in jq_trades) / jqty
    ap = sum(Decimal(str(t["price"])) * t["quantity"] for t in aq_trades) / aqty
    tolerance = Decimal("0.000000001")
    if (
        abs(Decimal(str(jo["avg_cost"])) - Decimal(str(position["avg_cost"]))) > tolerance
        or abs(jp - ap) > tolerance
        or abs(ap - Decimal(ao["average_price"])) > tolerance
    ):
        return None
    return dict(
        prior_position_snapshot_seq=seq,
        prior_position_cost=decimal(position["avg_cost"]),
        jq_order_avg_cost=decimal(jo["avg_cost"]),
        jq_trade_average=decimal(jp),
        aq_order_average_fill_price=ao["average_price"],
        aq_trade_average=decimal(ap),
        filled_quantity=aqty,
        general_field_semantics_certified=False,
    )


def compare(case_binding, aq_root):
    if case_binding["case_id"] not in IDS:
        raise ValueError("CARRY_COMPARISON_SCOPE_REQUIRED")
    report = compare_d1(case_binding, aq_root)  # includes all artifact/reference hash checks
    root, folder = Path(aq_root), Path(case_binding["folder"])
    jq = [
        json.loads(line, parse_float=Decimal)
        for line in (folder / "events.jsonl").read_text().splitlines()
    ]
    aq = [
        json.loads(line, parse_float=Decimal)
        for line in (root / "events.jsonl").read_text().splitlines()
    ]
    result = read_json(root / "result.json")
    if result.get("research_classification") != "CONDITIONAL_RESEARCH_EXECUTION":
        raise ValueError("CARRY_ACTUAL_CONDITIONAL_RUN_REQUIRED")
    left_by_seq, right_by_seq = {e["seq"]: e for e in jq}, {e["seq"]: e for e in aq}
    for row in report["comparisons"]:
        match = re.fullmatch(r"/orders/(\d+)/average_price", row["field"])
        if (
            not match
            or not row["jq_present"]
            or not row["aq_present"]
            or row["status"] in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
        ):
            continue
        left, right = left_by_seq[row["jq_event_seq"]], right_by_seq[row["aq_event_seq"]]
        evidence = sell_cost_evidence(left, right, jq, int(match[1]))
        if evidence:
            row.update(
                classification="OBSERVED_SELL_COST_FIELD_VS_FILL_PRICE",
                evidence=evidence,
                explanation="This JQ Order.avg_cost equals prior position cost while AQ Order.avg_price equals its fills. Both trade-price records match within recorded float precision. The two order fields are retained, not coerced into equal semantics.",
            )
    for left in [e for e in jq if e["kind"] == "snapshot"]:
        candidates = [
            e
            for e in aq
            if e["kind"] == "snapshot"
            and e["label"] == left["label"]
            and (clock(e["at"]) == clock(left["time"]) or left["label"] == "strategy_end")
        ]
        if len(candidates) != 1:
            raise ValueError("CARRY_SNAPSHOT_BINDING_AMBIGUOUS")
        right = candidates[0]
        lv = {"account": {"transferable_cash": decimal(left["portfolio"]["transferable_cash"])}}
        rv = {"account": {"transferable_cash": decimal(right["account"]["transferable_cash"])}}
        rows = fields(
            lv, rv, dict(label=left["label"], jq_event_seq=left["seq"], aq_event_seq=right["seq"])
        )
        for row in rows:
            attribute(row, left["label"], case_binding["case_id"])
            account = right["account"]
            pending = Decimal(account["cash_settlement"]["unsettled_sale_cash"])
            if (
                row["status"] == "DIFFERENT"
                and left["label"] == "strategy_end"
                and pending > 0
                and Decimal(row["jq"]) == Decimal(str(left["portfolio"]["available_cash"]))
                and Decimal(row["jq"]) == Decimal(account["available_cash"])
                and Decimal(row["aq"]) == Decimal(account["available_cash"]) - pending
            ):
                row.update(
                    classification="OBSERVED_TERMINAL_WITHDRAWABLE_PRESENTATION",
                    explanation="At this terminal callback JQ shows all available cash as transferable; AQ retains unspent sale receipts until the next admitted session. No additional session or JQ internal settlement phase is inferred.",
                    evidence=dict(
                        native_unsettled_sale_cash=decimal(pending), next_session_not_executed=True
                    ),
                )
        report["comparisons"].extend(rows)
    if case_binding["case_id"] in {"B22", "B33"}:
        kinds = ["day_start"] + (
            ["callback", "sell_precondition"] if case_binding["case_id"] == "B22" else []
        )
        for kind in kinds:
            lefts, rights = (
                [e for e in jq if e["kind"] == kind],
                [e for e in aq if e["kind"] == kind],
            )
            report["comparisons"].extend(
                fields({"count": len(lefts)}, {"count": len(rights)}, {"label": kind + "_sequence"})
            )
            for left, right in zip(lefts, rights):
                keys = {
                    "day_start": ["previous_date"],
                    "callback": ["name"],
                    "sell_precondition": ["closeable_amount"],
                }[kind]
                lv, rv = {k: left[k] for k in keys}, {k: right[k] for k in keys}
                lv["at"], rv["at"] = clock(left["time"]), clock(right["at"])
                report["comparisons"].extend(
                    fields(
                        lv,
                        rv,
                        dict(label=kind, jq_event_seq=left["seq"], aq_event_seq=right["seq"]),
                    )
                )
    report["differences"] = [
        r
        for r in report["comparisons"]
        if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    report["unresolved_count"] = sum(
        r.get("classification") in {None, "REQUIRES_INVESTIGATION"} for r in report["differences"]
    )
    report["status"] = (
        "CONDITIONAL_DIFFERENCES_NEED_INVESTIGATION"
        if report["unresolved_count"]
        else "CONDITIONAL_DIFFERENCES_ATTRIBUTED"
    )
    report.update(
        research_model=result["model"],
        known_facts=result["known_facts"],
        assumptions=result["assumptions"],
        data_gaps=result["data_gaps"],
        unknowns=result["unknowns"],
        no_blanket_pass=True,
        original_requests_submitted=result["original_requests_submitted"],
        native_liquidation_verified=result["native_liquidation_verified"],
    )
    report["uncompared_fields"] = [
        x for x in report["uncompared_fields"] if "transferable_cash" not in x
    ]
    report["uncompared_fields"].append(
        "JQ acc_avg_cost/hold_cost and missing native cancel/finish-time fields remain outside equivalent-field certification"
    )
    report["market_queries"] = compare_markets(jq, aq)
    if report["market_queries"]["fields"] == 0:
        report["market_queries"]["status"] = "NO_SEPARATE_MARKET_OBSERVATION_EVENTS"
    elif report["market_queries"]["differences"]:
        report["status"] = "CONDITIONAL_DIFFERENCES_NEED_INVESTIGATION"
        report["unresolved_count"] += len(report["market_queries"]["differences"])
    return report
