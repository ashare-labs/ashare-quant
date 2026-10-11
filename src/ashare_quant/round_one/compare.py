"""Read-only field comparison; reference outputs never enter engine input paths."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ashare_quant.contracts import SHANGHAI


def read_json(path):
    return json.loads(Path(path).read_text(), parse_float=Decimal)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clock(value):
    result = datetime.fromisoformat(value)
    return (
        result.replace(tzinfo=SHANGHAI).isoformat() if result.tzinfo is None else result.isoformat()
    )


def decimal(value):
    return format(Decimal(str(value)), "f")


def semantic_status(status, filled, requested):
    if status in {"canceled", "CANCELLED"}:
        return "cancelled"
    if status in {"held", "FILLED"} and filled == requested:
        return "filled"
    if status in {"filled", "ACTIVE"} and 0 < filled < requested:
        return "partial_active"
    if status in {"new", "ACTIVE", "PENDING_NEW"} and filled == 0:
        return "accepted_unfilled"
    if status in {"rejected", "REJECTED"}:
        return "rejected"
    return "unmapped:" + status


def normalize_order(order, side):
    jq = side == "jq"
    quantity = order["amount"] if jq else order["requested"]
    filled = order["filled"]
    value = {
        "security": order["security"],
        "quantity": quantity,
        "filled": filled,
        "remaining": quantity - filled,
        "average_price": decimal(order["avg_cost"] if jq else order["average_price"]),
        "transaction_cost": decimal(order["commission"] if jq else order["transaction_cost"]),
        "submitted_at": clock(order["add_time"] if jq else order["submitted_at"]),
        "status": semantic_status(
            order["status"] if jq else order["native_status"], filled, quantity
        ),
    }
    # B08 exposes style only in the recorded representation. Parse the specific
    # field as text, never eval a platform object or infer market from absence.
    style = order.get("style", "")
    if jq:
        if not style:
            match = re.search(
                r"'style': (LimitOrderStyle: _limit_price=[0-9.]+|MarketOrderStyle)(?:,|})",
                order.get("native_repr", ""),
            )
            style = match.group(1) if match else ""
        if "LimitOrderStyle" in style:
            value["style"] = "limit"
        elif "MarketOrderStyle" in style:
            value["style"] = "market"
        match = re.search(r"_limit_price=([0-9.]+)", style)
        if value.get("style") == "limit" and match:
            value["limit_price"] = decimal(match.group(1))
    else:
        value["style"] = style.lower()
        if value["style"] == "limit":
            value["limit_price"] = decimal(order["limit_price"])
    return value


def normalize_snapshot(event, side):
    jq = side == "jq"
    account = event["portfolio"] if jq else event["account"]
    value = {
        "at": clock(event["time"] if jq else event["at"]),
        "account": {
            k: decimal(account["locked_cash" if k == "frozen_cash" and jq else k])
            for k in ("available_cash", "frozen_cash", "positions_value", "total_value")
        },
        "open_order_count": len(event["open_order_ids"]),
        "orders": [normalize_order(o, side) for o in event["orders"]],
    }
    positions = event["positions"] if jq else account["positions"]
    value["positions"] = sorted(
        [
            {
                "security": p["security"],
                "quantity": p["total_amount"] if jq else p["quantity"],
                "sellable": p["closeable_amount"] if jq else p["sellable"],
                "average_price": decimal(p["avg_cost"] if jq else p["average_price"]),
                "last_price": decimal(p["price"] if jq else p["last_price"]),
                "market_value": decimal(p["value"] if jq else p["market_value"]),
            }
            for p in positions
        ],
        key=lambda p: p["security"],
    )
    value["trades"] = [
        {
            "security": t["security"],
            "quantity": t["amount"] if jq else t["quantity"],
            "price": decimal(t["price"]),
            "at": clock(t["time"] if jq else t["at"]),
        }
        for t in event["trades"]
    ]
    return value


def flatten(value, prefix=""):
    if isinstance(value, dict):
        return {k2: v2 for k, v in value.items() for k2, v2 in flatten(v, prefix + "/" + k).items()}
    if isinstance(value, list):
        result = {prefix + "/count": len(value)}
        for index, v in enumerate(value):
            result.update(flatten(v, prefix + "/" + str(index)))
        return result
    return {prefix: value}


def fields(jq, aq, context):
    left, right = flatten(jq), flatten(aq)
    results = []
    for name in sorted(set(left) | set(right)):
        item = {
            **context,
            "field": name,
            "jq": left.get(name),
            "aq": right.get(name),
            "jq_present": name in left,
            "aq_present": name in right,
        }
        if name not in left or name not in right:
            item["status"] = "FIELD_NOT_OBSERVED"
        else:
            same_type = not (isinstance(left[name], bool) or isinstance(right[name], bool)) or type(
                left[name]
            ) is type(right[name])
            item["status"] = "MATCH" if same_type and left[name] == right[name] else "DIFFERENT"
            if item["status"] == "DIFFERENT":
                try:
                    delta = Decimal(str(right[name])) - Decimal(str(left[name]))
                    item["numeric_delta"] = decimal(delta)
                    if delta == 0:
                        item["status"] = "MATCH_NUMERIC_DIFFERENT_TEXT"
                    elif abs(delta) <= Decimal("0.000000001"):
                        item["classification"] = "FLOAT_REPRESENTATION_DIFFERENCE_RETAINED"
                except Exception:
                    pass
        results.append(item)
    return results


def attribute(row, label, case_id, reservation_proven=False, pending_view_proven=False):
    if row["status"] in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}:
        return
    path = row["field"]
    if row.get("classification"):
        return
    if (
        label == "strategy_end"
        and path == "/at"
        and (datetime.fromisoformat(row["jq"]) - datetime.fromisoformat(row["aq"])).total_seconds()
        == 0.001
    ):
        row["classification"] = "TERMINAL_CLOCK_OFFSET"
        row["explanation"] = "The recorded terminal callbacks differ by 1ms."
    elif (
        label == "strategy_end"
        and (path.startswith("/orders/") or path.startswith("/trades/"))
        and (not row["jq_present"] or (path.endswith("/count") and row["jq"] == 0))
    ):
        row["classification"] = "TERMINAL_COLLECTION_RETENTION"
        row["explanation"] = (
            "JQ clears daily orders/trades; AQ retains cumulative native objects in its read-only projection."
        )
    elif (
        label == "strategy_end"
        and re.fullmatch(r"/positions/[0-9]+/sellable", path)
        and row["aq"] == 0
        and row["jq"] > 0
    ):
        row["classification"] = "TERMINAL_SELLABLE_ROLLOVER"
        row["explanation"] = (
            "JQ has advanced closeable_amount at strategy end; native RQAlpha resets _non_closable at the next before_trading, not settlement. No next-session event is fabricated."
        )
    elif (
        case_id == "B10"
        and label == "before_cancel"
        and reservation_proven
        and path in {"/account/available_cash", "/account/frozen_cash"}
    ):
        row["classification"] = "NATIVE_CASH_RESERVATION_POLICY"
        row["explanation"] = (
            "AQ native account releases filled quantity times frozen limit price; JQ snapshot retains the initial reservation less actual execution cost until cancellation. Neither record is rewritten."
        )
    elif (
        case_id == "B12"
        and path == "/open_order_count"
        and label in {"after_submit", "opening_checkpoint"}
        and row["jq"] == 0
        and row["aq"] == 1
    ):
        row["classification"] = "OPEN_ORDER_QUERY_PHASE_SEMANTICS"
        row["explanation"] = (
            "RQAlpha exposes its active pre-open order; JQ get_open_orders is empty while get_orders contains the accepted new order. Both retain cash reservation and no fill."
        )
    elif (
        case_id == "B20"
        and pending_view_proven
        and path == "/open_order_count"
        and row["jq"] == 0
        and row["aq"] == 1
    ):
        row["classification"] = "LUNCH_PENDING_ORDER_QUERY_VIEW"
        row["explanation"] = (
            "Only open-order query membership differs: both snapshots retain the same accepted unfilled "
            "order, reservation and empty trade/position state. JQ's pending lunch order is absent "
            "from get_open_orders; AQ exposes its native active broker order. This does not establish "
            "JQ internal queue mechanics or generalize beyond these four snapshots."
        )
    else:
        row["classification"] = "REQUIRES_INVESTIGATION"


def pending_order_view_proven(jq, aq):
    if (jq["open_order_count"], aq["open_order_count"]) != (0, 1):
        return False
    if len(jq["orders"]) != 1 or len(aq["orders"]) != 1:
        return False
    if jq["trades"] or aq["trades"] or jq["positions"] or aq["positions"]:
        return False
    if jq["orders"][0]["filled"] != 0 or aq["orders"][0]["filled"] != 0:
        return False
    left = {k: v for k, v in jq.items() if k != "open_order_count"}
    right = {k: v for k, v in aq.items() if k != "open_order_count"}
    return all(
        r["status"] in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"} for r in fields(left, right, {})
    )


def reservation_policy_proven(jq, aq, initial_cash):
    """Attribute only the exact observed zero-fee reservation equations."""
    if len(jq["orders"]) != 1 or len(aq["orders"]) != 1:
        return False
    order = aq["orders"][0]
    if (
        any(
            r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
            for r in fields(jq["orders"][0], order, {})
        )
        or order.get("style") != "limit"
        or Decimal(order["transaction_cost"]) != 0
    ):
        return False
    limit, price = Decimal(order["limit_price"]), Decimal(order["average_price"])
    cost = order["filled"] * price
    jq_frozen = order["quantity"] * limit - cost
    aq_frozen = order["remaining"] * limit
    return all(
        Decimal(snapshot["account"]["frozen_cash"]) == frozen
        and Decimal(snapshot["account"]["available_cash"]) == Decimal(initial_cash) - cost - frozen
        for snapshot, frozen in ((jq, jq_frozen), (aq, aq_frozen))
    )


def normalize_intent_pair(left, right):
    if "requested" in left:
        # The original D5 request is distinct from the engine-sized order.
        # JQ did not log style in this event; order objects compare it separately.
        return (
            {
                "api": left["api"],
                "requested": decimal(left["requested"]),
                "security": left["symbol"],
                "at": clock(left["time"]),
            },
            {
                "api": right["api"],
                "requested": decimal(right["requested"]),
                "security": right["security"],
                "at": clock(right["at"]),
            },
        )
    lvalue = {
        "quantity": left["amount"],
        "at": clock(left["time"]),
        "style": {"LimitOrderStyle": "limit", "MarketOrderStyle": "market"}[left["style"]],
    }
    rvalue = {"quantity": right["quantity"], "at": clock(right["at"]), "style": right["style"]}
    if "limit_price" in left:
        lvalue["limit_price"] = decimal(left["limit_price"])
    if right["limit_price"] is not None:
        rvalue["limit_price"] = decimal(right["limit_price"])
    return lvalue, rvalue


def compare_d1(case_binding, aq_root):
    folder = Path(case_binding["folder"])
    for name in ("events.jsonl", "jq.py", "reference.json"):
        if sha(folder / name) != case_binding["files"][name]:
            raise ValueError("ROUND_ONE_REFERENCE_CHANGED:" + name)
    aq_root = Path(aq_root)
    complete = read_json(aq_root / "complete.json")
    required = {
        "account.json",
        "config.json",
        "events.jsonl",
        "manifest.json",
        "native-config.json",
        "native.log",
        "orders.json",
        "result.json",
        "strategy.py",
        "teardown.json",
        "trades.json",
    }
    if set(complete["artifacts"]) != required:
        raise ValueError("ROUND_ONE_INCOMPLETE_AQ_SEAL")
    for name, checksum in complete["artifacts"].items():
        if sha(aq_root / name) != checksum:
            raise ValueError("ROUND_ONE_AQ_ARTIFACT_CHANGED:" + name)
    jq_events = [
        json.loads(line, parse_float=Decimal)
        for line in (folder / "events.jsonl").read_text().splitlines()
    ]
    aq_result = read_json(aq_root / "result.json")
    aq_config = read_json(aq_root / "config.json")
    if (
        aq_config["case_id"] != case_binding["case_id"]
        or complete["identity"] != aq_result["identity"]
    ):
        raise ValueError("ROUND_ONE_CASE_IDENTITY_MISMATCH")
    aq_events = [
        read_json_line(line) for line in (aq_root / "events.jsonl").read_text().splitlines()
    ]
    if not aq_result["actual_real_case_run"] or not aq_result["scenario_triggered"]:
        raise ValueError("ROUND_ONE_EXECUTED_REAL_CASE_REQUIRED")
    jq_snapshots = [e for e in jq_events if e["kind"] == "snapshot"]
    aq_snapshots = [e for e in aq_events if e["kind"] == "snapshot"]
    comparisons = []
    for left in jq_snapshots:
        label = "opening_checkpoint" if left["label"] == "auction_checkpoint" else left["label"]
        candidates = [r for r in aq_snapshots if r["label"] == label]
        if len(candidates) > 1:
            candidates = [r for r in candidates if clock(r["at"]) == clock(left["time"])]
        if len(candidates) != 1:
            raise ValueError("ROUND_ONE_SNAPSHOT_BINDING_AMBIGUOUS:" + label)
        right = candidates[0]
        normalized_left, normalized_right = (
            normalize_snapshot(left, "jq"),
            normalize_snapshot(right, "aq"),
        )
        reservation_proven = label == "before_cancel" and reservation_policy_proven(
            normalized_left, normalized_right, aq_config["initial_cash"]
        )
        rows = fields(
            normalized_left,
            normalized_right,
            {"label": label, "jq_event_seq": left["seq"], "aq_event_seq": right["seq"]},
        )
        for row in rows:
            attribute(
                row,
                label,
                case_binding["case_id"],
                reservation_proven,
                case_binding["case_id"] == "B20"
                and pending_order_view_proven(normalized_left, normalized_right),
            )
        comparisons.extend(rows)
    for left_kind, right_kind in (("order_return", "order_return"), ("decision", "order_request")):
        left_events = [e for e in jq_events if e["kind"] == left_kind]
        right_events = [e for e in aq_events if e["kind"] == right_kind]
        comparisons.extend(
            fields(
                {"count": len(left_events)},
                {"count": len(right_events)},
                {"label": right_kind + "_sequence"},
            )
        )
        for left_event, right_event in zip(left_events, right_events):
            if left_kind == "order_return":
                left_value = {"returned_none": left_event["returned_none"]}
                right_value = {"returned_none": right_event["returned_none"]}
            else:
                left_value, right_value = normalize_intent_pair(left_event, right_event)
            comparisons.extend(
                fields(
                    left_value,
                    right_value,
                    {
                        "label": right_kind,
                        "jq_event_seq": left_event["seq"],
                        "aq_event_seq": right_event["seq"],
                    },
                )
            )
    if case_binding["case_id"] in {"B20", "B35", "B36", "B37", "B38", "B39"}:
        left_callbacks = [e for e in jq_events if e["kind"] == "callback"]
        right_callbacks = [e for e in aq_events if e["kind"] == "callback"]
        comparisons.extend(
            fields(
                {"count": len(left_callbacks)},
                {"count": len(right_callbacks)},
                {"label": "callback_sequence"},
            )
        )
        for left_event, right_event in zip(left_callbacks, right_callbacks):
            lvalue = {
                k: left_event[k]
                for k in ("name", "state_counter", "previous_date")
                if k in left_event
            }
            rvalue = {k: right_event.get(k) for k in lvalue}
            lvalue["at"], rvalue["at"] = clock(left_event["time"]), clock(right_event["at"])
            label = (
                "strategy_end" if left_event["name"] == "on_strategy_end" else left_event["name"]
            )
            rows = fields(
                lvalue,
                rvalue,
                {
                    "label": label,
                    "jq_event_seq": left_event["seq"],
                    "aq_event_seq": right_event["seq"],
                },
            )
            for row in rows:
                if row["field"] == "/previous_date" and row["aq"] is None:
                    row["classification"] = "OWNER_ADJACENCY_UNKNOWN"
                    row["explanation"] = (
                        "AQ declines to invent a prior exchange date; this field is not validated."
                    )
                else:
                    attribute(row, label, case_binding["case_id"])
            comparisons.extend(rows)
    if case_binding["case_id"] == "B35":
        for kind in ("state_initialized", "state_update"):
            left_events = [e for e in jq_events if e["kind"] == kind]
            right_events = [e for e in aq_events if e["kind"] == kind]
            comparisons.extend(
                fields({"count": len(left_events)}, {"count": len(right_events)}, {"label": kind})
            )
            for left_event, right_event in zip(left_events, right_events):
                keys = (
                    ("counter", "created")
                    if kind == "state_initialized"
                    else ("before", "after", "state_created")
                )
                left_value = {
                    k: clock(left_event[k]) if k in {"created", "state_created"} else left_event[k]
                    for k in keys
                }
                right_value = {
                    k: clock(right_event[k])
                    if k in {"created", "state_created"}
                    else right_event[k]
                    for k in keys
                }
                left_value["at"], right_value["at"] = (
                    clock(left_event["time"]),
                    clock(right_event["at"]),
                )
                comparisons.extend(
                    fields(
                        left_value,
                        right_value,
                        {
                            "label": kind,
                            "jq_event_seq": left_event["seq"],
                            "aq_event_seq": right_event["seq"],
                        },
                    )
                )
    differences = [
        r for r in comparisons if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    unresolved = [
        r for r in differences if r.get("classification") in {None, "REQUIRES_INVESTIGATION"}
    ]
    return {
        "schema": "aq.round-one.case-diff.v1",
        "case_id": case_binding["case_id"],
        "dimension": case_binding["dimension"],
        "reference_verdict": case_binding["reference_verdict"],
        "status": "DIFFERENCES_ATTRIBUTED" if not unresolved else "DIFFERENCES_NEED_INVESTIGATION",
        "aq_run_identity": aq_result["identity"],
        "aq_run_root": str(aq_root),
        "jq_source": str(folder / "events.jsonl"),
        "jq_events_sha256": sha(folder / "events.jsonl"),
        "aq_complete_sha256": sha(aq_root / "complete.json"),
        "comparisons": comparisons,
        "differences": differences,
        "unresolved_count": len(unresolved),
        "real_run_reused": case_binding["case_id"] in {"B08", "B09", "B10", "B11", "B12", "B13"},
        "scope": "observed_scenario_only_not_full_semantic_validation",
        "historical_visibility_verdict": "NOT_CERTIFIED_SEPARATE_FROM_JQ_COMPATIBILITY",
        "jq_new_run": False,
        "data_evidence_class": (
            "mixed_JQ_request_quote_and_independent_TDX_retrospective_points_under_explicit_model"
            if case_binding["case_id"] == "B20"
            else "shared_source_observation_contract_not_independent_market_crosscheck"
        ),
        "uncompared_fields": [
            "Opaque order/trade IDs are linked locally, not compared as equal IDs",
            "JQ fee components not observed: no comparison of inferred native components",
            "AQ has not exposed JQ transferable_cash/acc_avg_cost/hold_cost and native cancel/finish-time fields",
            "No independent auction matching claim for B12",
        ],
        "numeric_policy": "Exact Decimal comparison; sub-1e-9 differences retained and classified, never rounded away",
    }


def read_json_line(line):
    return json.loads(line, parse_float=Decimal)
