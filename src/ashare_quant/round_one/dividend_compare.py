"""B26 observed differences: gross posting is intentionally not reference net cash."""

import json
from decimal import Decimal
from pathlib import Path

from .compare import clock, compare_d1, decimal, fields, read_json
from .market_compare import compare_markets
from .query_compare import compare as compare_queries


def posting_evidence(before, after, native):
    jq_quantity = before["positions"][0]["total_amount"]
    if jq_quantity <= 0 or after["positions"][0]["total_amount"] != jq_quantity:
        return None
    b, a = native["before"], native["after"]
    if b["positions"][0]["quantity"] != jq_quantity or a["positions"][0]["quantity"] != jq_quantity:
        return None
    gross = Decimal(native["source_gross_per_share"]) * jq_quantity
    aq_credit = Decimal(a["cash"]) - Decimal(b["cash"])
    jq_credit = Decimal(str(after["portfolio"]["available_cash"])) - Decimal(
        str(before["portfolio"]["available_cash"])
    )
    if abs(aq_credit - gross) > Decimal("1e-9") or not 0 < jq_credit < gross:
        return None
    raw = Decimal(b["positions"][0]["last_price"])
    mark = Decimal(a["positions"][0]["last_price"])
    if abs(mark - (raw - Decimal(native["source_gross_per_share"]))) > Decimal("1e-9"):
        return None
    return dict(
        actual_quantity=jq_quantity,
        native_gross_credit=decimal(aq_credit),
        jq_observed_credit=decimal(jq_credit),
        cash_difference=decimal(aq_credit - jq_credit),
        native_raw_prior_mark=decimal(raw),
        native_adjusted_mark=decimal(mark),
        jq_observed_open_mark=decimal(after["positions"][0]["price"]),
        exact_jq_rounding_rule_certified=False,
        account_specific_tax_certified=False,
        official_intraday_payment_time_certified=False,
    )


def compare(binding, root):
    if binding["case_id"] != "B26":
        raise ValueError("B26_COMPARISON_SCOPE_REQUIRED")
    report = compare_d1(binding, root)
    result = read_json(Path(root) / "result.json")
    if (
        result["research_classification"] != "CONDITIONAL_KNOWN_CASH_DIVIDEND_EXECUTION"
        or not result["scenario_triggered"]
    ):
        raise ValueError("B26_ACTUAL_DIVIDEND_SCENARIO_REQUIRED")
    jq = [
        json.loads(s, parse_float=Decimal)
        for s in (Path(binding["folder"]) / "events.jsonl").read_text().splitlines()
    ]
    aq = [
        json.loads(s, parse_float=Decimal)
        for s in (Path(root) / "events.jsonl").read_text().splitlines()
    ]
    jbefore = next(
        e
        for e in jq
        if e.get("label") == "end_of_day" and clock(e["time"]) == "2024-06-13T15:30:00+08:00"
    )
    jafter = next(
        e
        for e in jq
        if e.get("label") == "day_start" and clock(e["time"]) == "2024-06-14T09:00:00+08:00"
    )
    native = result["native_dividend_transitions"]
    if len(native) != 1:
        raise ValueError("B26_SINGLE_NATIVE_POSTING_REQUIRED")
    proof = posting_evidence(jbefore, jafter, native[0])
    pairs = {}
    for left in [e for e in jq if e["kind"] == "snapshot"]:
        rights = [
            e
            for e in aq
            if e["kind"] == "snapshot"
            and e["label"] == left["label"]
            and (clock(e["at"]) == clock(left["time"]) or left["label"] == "strategy_end")
        ]
        if len(rights) != 1:
            raise ValueError("B26_SNAPSHOT_CONTEXT_AMBIGUOUS")
        right = rights[0]
        pairs[left["seq"]] = (left, right)
        report["comparisons"].extend(
            fields(
                {"account": {"transferable_cash": decimal(left["portfolio"]["transferable_cash"])}},
                {"account": {"transferable_cash": decimal(right["account"]["transferable_cash"])}},
                dict(label=left["label"], jq_event_seq=left["seq"], aq_event_seq=right["seq"]),
            )
        )
    lefts, rights = ([e for e in v if e["kind"] == "day_start"] for v in (jq, aq))
    report["comparisons"].extend(
        fields({"count": len(lefts)}, {"count": len(rights)}, dict(label="day_start_sequence"))
    )
    for left, right in zip(lefts, rights):
        report["comparisons"].extend(
            fields(
                dict(at=clock(left["time"]), previous_date=left["previous_date"]),
                dict(at=clock(right["at"]), previous_date=right["previous_date"]),
                dict(label="day_start", jq_event_seq=left["seq"], aq_event_seq=right["seq"]),
            )
        )
    for row in report["comparisons"]:
        if row["status"] != "DIFFERENT" or row.get("jq_event_seq") not in pairs or proof is None:
            continue
        left, right = pairs[row["jq_event_seq"]]
        if clock(left["time"]) < "2024-06-14T09:00:00+08:00":
            continue
        field = row["field"]
        kind = None
        delta = Decimal(row["numeric_delta"]) if "numeric_delta" in row else None
        cash = Decimal(right["account"]["available_cash"]) - Decimal(
            str(left["portfolio"]["available_cash"])
        )
        positions = Decimal(right["account"]["positions_value"]) - Decimal(
            str(left["portfolio"]["positions_value"])
        )
        if abs(cash - Decimal(proof["cash_difference"])) > Decimal("1e-9"):
            continue
        if field in {"/account/available_cash", "/account/transferable_cash"} and delta == cash:
            kind = "EXPLICIT_GROSS_VS_OBSERVED_NET_CREDIT"
        elif field == "/account/total_value" and abs(delta - cash - positions) < Decimal("1e-8"):
            kind = "GROSS_CREDIT_AND_MARK_VALUE_COMPOSITION"
        elif clock(left["time"]) == "2024-06-14T09:00:00+08:00" and field in {
            "/positions/0/last_price",
            "/positions/0/market_value",
            "/account/positions_value",
        }:
            native_mark = Decimal(proof["native_adjusted_mark"])
            if abs(Decimal(right["account"]["positions"][0]["last_price"]) - native_mark) < Decimal(
                "1e-9"
            ):
                kind = "NATIVE_UNROUNDED_EX_MARK_VS_OBSERVED_JQ_MARK"
        if kind:
            row.update(
                classification=kind,
                explanation="Retained observed difference under explicit gross native model; no inferred account tax or JQ rounding rule.",
                evidence=proof,
            )
    report["differences"] = [
        r
        for r in report["comparisons"]
        if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    report["unresolved_count"] = sum(
        r.get("classification") in {None, "REQUIRES_INVESTIGATION"} for r in report["differences"]
    )
    report.update(
        status="CONDITIONAL_DIVIDEND_EXECUTED_WITH_RETAINED_DIFFERENCES",
        no_blanket_pass=True,
        posting_evidence=proof,
        model=result["model"],
        known_facts=result["known_facts"],
        cash_fact=result["cash_fact"],
        assumptions=result["assumptions"],
        unknowns=result["unknowns"],
        data_gaps=result["data_gaps"],
        native_queries=compare_queries(binding, root),
        market_queries=compare_markets(jq, aq),
        query_evidence="same-source snapshot fidelity, not independent market validation",
        point_evidence="June14 10:00 native valuation uses independently obtained retrospective TDX point; source label mapping is not JQ quote/PIT equivalence",
    )
    report["uncompared_fields"] = [
        x for x in report["uncompared_fields"] if "transferable_cash" not in x
    ]
    report["uncompared_fields"].extend(
        [
            "JQ other cost fields and native cancel/finish timestamps not certified",
            "Account-specific tax and exact official payment/rounding rules remain unknown",
        ]
    )
    return report
