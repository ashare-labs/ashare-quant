"""Versioned review of sealed comparisons; no output enters the trading engine."""

import ast
import copy
import re
from datetime import datetime
from decimal import Decimal

from .compare import clock, fields

READOUT_CALLS = {
    "'\\n'.join",
    "content.encode",
    "context.portfolio.positions.values",
    "dict",
    "g.events.append",
    "get_open_orders",
    "get_open_orders().keys",
    "get_orders",
    "get_orders().values",
    "get_trades",
    "get_trades().values",
    "getattr",
    "hasattr",
    "hashlib.sha256",
    "hashlib.sha256(content.encode('utf-8')).hexdigest",
    "isinstance",
    "json.dumps",
    "len",
    "list",
    "log.error",
    "log.info",
    "math.isfinite",
    "orders.values",
    "repr",
    "str",
    "trades.values",
    "type",
    "value.item",
    "value.items",
    "write_file",
}


def terminal_call_graph(source):
    """Inspect only this fixed source's terminal callback; not a general Python sandbox."""
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    visited, calls = set(), set()

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Call):
                call = ast.unparse(node.func)
                calls.add(call)
                if call in funcs:
                    visit(call)

    if "on_strategy_end" not in funcs:
        raise ValueError("TERMINAL_CALLBACK_MISSING")
    visit("on_strategy_end")
    return dict(
        functions=sorted(visited),
        calls=sorted(calls),
        unreviewed_calls=sorted(calls - set(funcs) - READOUT_CALLS),
        scope="fixed trusted original source readout/export call graph; not proof for arbitrary code",
    )


def terminal_review(source, jq, aq):
    graph = terminal_call_graph(source)
    query_sites = [
        dict(function=f.name, line=n.lineno, call=ast.unparse(n))
        for f in ast.parse(source).body
        if isinstance(f, ast.FunctionDef)
        for n in ast.walk(f)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "get_open_orders"
    ]
    ends = []
    for events, key in ((jq, "time"), (aq, "at")):
        rows = [e for e in events if e["kind"] == "snapshot" and e["label"] == "strategy_end"]
        if len(rows) != 1:
            raise ValueError("TERMINAL_SNAPSHOT_AMBIGUOUS")
        ends.append(datetime.fromisoformat(clock(rows[0][key])))
    begin, end = sorted(ends)
    economic = {
        "native_trade",
        "native_order_event",
        "native_dividend_transition",
        "decision",
        "order_request",
        "order_return",
        "cancel_request",
        "cancel_return",
        "trade",
        "trade_created",
        "order_created",
        "fill",
        "dividend_posted",
    }
    intervals = []
    after = []
    last_business = []
    for side, events, key in (("jq", jq, "time"), ("aq", aq, "at")):
        intervals.append(
            dict(
                side=side,
                events=[
                    dict(seq=e["seq"], kind=e["kind"], at=clock(e[key]))
                    for e in events
                    if begin <= datetime.fromisoformat(clock(e[key])) <= end
                ],
            )
        )
        mutations = [e for e in events if e["kind"] in economic]
        after.extend(
            dict(side=side, seq=e["seq"], kind=e["kind"], at=clock(e[key]))
            for e in mutations
            if datetime.fromisoformat(clock(e[key])) >= begin
        )
        prior = [e for e in events if datetime.fromisoformat(clock(e[key])) < begin]
        last_business.append(
            dict(
                side=side,
                last_prior_seq=prior[-1]["seq"] if prior else None,
                last_prior_at=clock(prior[-1][key]) if prior else None,
            )
        )
    matched_offset = (ends[0] - ends[1]).total_seconds() == 0.001
    reviewed = matched_offset and not graph["unreviewed_calls"] and not after
    return dict(
        jq_at=ends[0].isoformat(),
        aq_at=ends[1].isoformat(),
        delta_seconds=str(ends[0] - ends[1]),
        graph=graph,
        open_order_query_sites=query_sites,
        interval_events=intervals,
        economic_events_at_or_after_terminal=after,
        last_preterminal_events=last_business,
        verdict="FIXED_TERMINAL_READOUT_NO_ORDERING_EFFECT" if reviewed else "REQUIRES_REVIEW",
        limitation="Only these sealed final readout callbacks; no blanket 1ms tolerance. Terminal account/query view differences remain separate.",
    )


def correct_sell_price_fields(report, jq, aq):
    report = copy.deepcopy(report)
    lefts, rights = ({e["seq"]: e for e in events} for events in (jq, aq))
    corrections = []
    for i, row in enumerate(report["comparisons"]):
        match = re.fullmatch(r"/orders/(\d+)/average_price", row["field"])
        if not match or not row["jq_present"] or not row["aq_present"]:
            continue
        ix = int(match[1])
        left = lefts[row["jq_event_seq"]]
        right = rights[row["aq_event_seq"]]
        lo, ro = left["orders"][ix], right["orders"][ix]
        if lo.get("is_buy") is not False:
            continue
        if ro.get("side") != "SELL" or "price" not in lo or lo["filled"] <= 0:
            raise ValueError("SELL_PRICE_FIELD_NOT_SUPPORTED")
        lt = [t for t in left["trades"] if t["order_id"] == lo["order_id"]]
        rt = [t for t in right["trades"] if t["order_id"] == ro["order_id"]]
        lp = sum(Decimal(str(t["price"])) * t["amount"] for t in lt) / lo["filled"]
        rp = sum(Decimal(str(t["price"])) * t["quantity"] for t in rt) / ro["filled"]
        if (
            sum(t["amount"] for t in lt) != lo["filled"]
            or sum(t["quantity"] for t in rt) != ro["filled"]
            or abs(lp - Decimal(str(lo["price"]))) > Decimal("0.000000001")
            or abs(rp - Decimal(str(ro["average_price"]))) > Decimal("0.000000001")
        ):
            raise ValueError("SELL_PRICE_TRADE_IDENTITY_FAILED")
        new = fields(
            {"value": str(lo["price"])},
            {"value": str(ro["average_price"])},
            {k: row[k] for k in ("label", "jq_event_seq", "aq_event_seq")},
        )[0]
        new["field"] = row["field"]
        if new["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"} and not new.get(
            "classification"
        ):
            new["classification"] = "ACTUAL_FILL_PRICE_DIFFERENCE"
        report["comparisons"][i] = new
        corrections.append(
            dict(
                old=row,
                corrected=new,
                jq_order_price=str(lo["price"]),
                jq_order_avg_cost=str(lo["avg_cost"]),
                jq_trade_average=str(lp),
                aq_trade_average=str(rp),
                cost_basis_projection_not_certified=True,
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
    report["order_field_corrections"] = corrections
    report["comparison_semantics"] = (
        "JQ Order.price vs native average fill price; Order.avg_cost retained as distinct sell cost basis"
    )
    report["field_semantics_source"] = dict(
        url="https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf",
        page_1_based=76,
        scope="legacy documentation corroborated by original order.price and trade rows; no claim of unchanged 2026 internals",
    )
    return report
