"""Offline semantic diff; this module is never imported by the native runner."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

PORTFOLIO = [
    "starting_cash",
    "inout_cash",
    "available_cash",
    "locked_cash",
    "positions_value",
    "total_value",
]
POSITION = [
    "security",
    "total_amount",
    "locked_amount",
    "closeable_amount",
    "today_amount",
    "price",
    "avg_cost",
    "value",
    "side",
]
ORDER = [
    "order_id",
    "security",
    "add_time",
    "is_buy",
    "amount",
    "filled",
    "status",
    "price",
    "avg_cost",
    "side",
    "action",
    "commission",
]
TRADE = ["trade_id", "order_id", "security", "time", "amount", "price"]


def load(path):
    events = [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]
    if not events:
        raise ValueError("SECOND_COMPARISON_EMPTY_INPUT:" + str(path))
    return events


def normalized(events):
    order_ids, trade_ids = {}, {}
    for e in events:
        if e["kind"] == "order_return" and e.get("order"):
            order_ids[str(e["order"]["order_id"])] = e["label"]

    def oid(value):
        key = str(value)
        if key not in order_ids:
            order_ids[key] = f"unbound_order_{len(order_ids) + 1}"
        return order_ids[key]

    def obj(v, fields):
        if v is None:
            return None
        out = {k: v[k] for k in fields if k in v}
        if "order_id" in out:
            out["order_id"] = oid(out["order_id"])
        if "trade_id" in out:
            t = str(out["trade_id"])
            if t not in trade_ids:
                count = sum(x.startswith(out["order_id"] + "/") for x in trade_ids.values())
                trade_ids[t] = out["order_id"] + "/" + str(count + 1)
            out["trade_id"] = trade_ids[t]
        return out

    result = {}
    excluded = []
    for e in events:
        kind = e["kind"]
        label = e.get("label", "")
        key = "|".join([e["time"][:19], kind, label, str(e.get("offset", ""))])
        if kind == "snapshot":
            out = dict(
                portfolio=obj(e["portfolio"], PORTFOLIO),
                positions=sorted(
                    [obj(p, POSITION) for p in e["positions"]], key=lambda p: p["security"]
                ),
                orders=sorted([obj(o, ORDER) for o in e["orders"]], key=lambda o: o["order_id"]),
                trades=sorted([obj(t, TRADE) for t in e["trades"]], key=lambda t: t["trade_id"]),
                open_order_ids=sorted(oid(i) for i in e["open_order_ids"]),
                observation_errors=e["observation_errors"],
            )
        elif kind in {
            "order_return",
            "cancel_lookup",
            "partial_state",
            "corporate_action_checkpoint",
        }:
            keep = {
                "returned_none",
                "present_in_orders",
                "present_in_open_orders",
                "distinct_trade_count",
                "two_fills_with_live_remainder",
                "observed_expected_quantity",
                "all_120_sellable",
            }
            out = {k: v for k, v in e.items() if k in keep}
            if "order" in e:
                out["order"] = obj(e["order"], ORDER)
            if "position" in e:
                out["position"] = obj(e["position"], POSITION)
            if "trades" in e:
                out["trades"] = sorted(
                    [obj(t, TRADE) for t in e["trades"]], key=lambda t: t["trade_id"]
                )
        elif kind in {
            "decision",
            "query_request",
            "query_return",
            "query_rows",
            "current_data",
            "precondition",
            "day_start",
            "not_triggered",
        }:
            out = {
                k: v
                for k, v in e.items()
                if k not in {"seq", "version", "case_id", "run_tag", "time", "kind", "label"}
            }
        elif kind == "query_exception":
            out = {"error_type": e["error_type"]}
        elif kind == "cancel_request":
            out = {"order_id": oid(e["order_id"])}
        elif kind in {"observation_error", "order_exception"}:
            out = {k: e[k] for k in ["error_type", "error"] if k in e}
        else:
            excluded.append(
                dict(
                    key=key,
                    reason="identity/export metadata or native diagnostic string",
                    kind=kind,
                )
            )
            continue
        if kind == "precondition" and "position" in out:
            out["position"] = obj(out["position"], POSITION)
        out["_event_time"] = e["time"]
        if key in result:
            raise ValueError("DUPLICATE_COMPARISON_KEY:" + key)
        result[key] = out
    return result, excluded


def compare(reference, actual, *, gaps=()):
    left, left_excluded = normalized(load(reference))
    right, right_excluded = normalized(load(actual))
    checks = []

    def add(key, path, a, b, missing=False):
        group = (
            "query_same_source"
            if "|query_" in key or "|current_data|" in key
            else "account_execution"
        )
        numeric = type(a) in (int, float) and type(b) in (int, float)
        same = (
            abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.00000001") if numeric else a == b
        )
        status = "difference" if missing or not same else "match"
        gap = None
        for g in gaps:
            clock_matches = g["at"][:19].replace("T", " ") == key[:19]
            portfolio_mark = path in {"/portfolio/positions_value", "/portfolio/total_value"}
            position_mark = False
            if path.startswith("/positions/") and path.endswith(("/price", "/value")):
                index = int(path.split("/")[2])
                positions = right.get(key, {}).get("positions", [])
                position_mark = (
                    index < len(positions) and positions[index]["security"] == g["security"]
                )
            if clock_matches and (portfolio_mark or position_mark):
                gap = g
                status = "unverified_missing_mark"
                break
        checks.append(
            dict(
                event=key,
                field=path,
                jq=a,
                aq=b,
                status=status,
                category=group,
                **({"gap": gap} if gap else {}),
            )
        )

    def walk(key, path, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(a.keys() | b.keys()):
                if k not in a or k not in b:
                    add(key, path + "/" + k, a.get(k), b.get(k), True)
                else:
                    walk(key, path + "/" + k, a[k], b[k])
        elif isinstance(a, list) and isinstance(b, list):
            add(key, path + "/length", len(a), len(b))
            for i in range(max(len(a), len(b))):
                if i >= len(a) or i >= len(b):
                    add(
                        key,
                        path + "/" + str(i),
                        a[i] if i < len(a) else None,
                        b[i] if i < len(b) else None,
                        True,
                    )
                else:
                    walk(key, path + "/" + str(i), a[i], b[i])
        else:
            add(key, path, a, b)

    for key in sorted(left.keys() | right.keys()):
        if key not in left or key not in right:
            add(key, "/", left.get(key), right.get(key), True)
        else:
            walk(key, "", left[key], right[key])
    return dict(
        schema="aq.second-batch.semantic-diff.v1",
        absolute_numeric_tolerance="1e-8; floating representation only",
        order_id_mapping="original submission label",
        trade_id_mapping="per-order encounter ordinal; not compared as shared ids",
        summary=dict(Counter(c["status"] for c in checks)),
        by_category={
            group: dict(Counter(c["status"] for c in checks if c["category"] == group))
            for group in ["query_same_source", "account_execution"]
        },
        checks=checks,
        excluded_events=dict(jq=left_excluded, aq=right_excluded),
        unsupported_field_scope=[
            "transferable_cash",
            "acc_avg_cost",
            "hold_cost",
            "position init/transact times",
            "unavailable order times/style/comment",
            "native_repr",
            "exact exception message text",
        ],
        correctness_and_scenario_trigger="must be assessed separately; match count is not a pass rate",
    )
