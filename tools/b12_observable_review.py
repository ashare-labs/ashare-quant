"""Offline B12 comparison of frozen raw events; no execution or market access."""

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "docs/round-one/b12-observable/evidence"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def events(path):
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1)), path
    return rows


def timestamp(value):
    if value is None:
        return None
    value = datetime.fromisoformat(value)
    return (value.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if value.tzinfo is None else value).isoformat()


def number(value):
    # Decimal equality, without rounding or tolerance against JQ answers.
    return Decimal(str(value))


def project(row, jq):
    account = row["portfolio" if jq else "account"]
    values = {"at": timestamp(row["time" if jq else "at"]),
              "available_cash": number(account["available_cash"]),
              "frozen_cash": number(account["locked_cash" if jq else "frozen_cash"]),
              "open_order_count": len(row["open_order_ids"]),
              "order_count": len(row["orders"]), "trade_count": len(row["trades"])}
    for i, order in enumerate(row["orders"]):
        fields = {"security": order["security"],
                  "status": order["status" if jq else "strategy_status"],
                  "submitted_at": timestamp(order["add_time" if jq else "submitted_at"]),
                  "requested": order["amount" if jq else "requested"], "filled": order["filled"],
                  "average_price": number(order["price" if jq else "average_price"]),
                  "finished_at": timestamp(order["finish_time" if jq else "finished_at"]),
                  "canceled_at": timestamp(order["cancel_time" if jq else "canceled_at"])}
        values.update({f"order.{i}.{k}": v for k, v in fields.items()})
    for i, trade in enumerate(row["trades"]):
        fields = {"security": trade["security"], "quantity": trade["amount" if jq else "quantity"],
                  "price": number(trade["price"]), "at": timestamp(trade["time" if jq else "at"])}
        values.update({f"trade.{i}.{k}": v for k, v in fields.items()})
    positions = row["positions"] if jq else account["positions"]
    values["position_count"] = len(positions)
    for i, position in enumerate(positions):
        values[f"position.{i}.security"] = position["security"]
        values[f"position.{i}.quantity"] = position["total_amount" if jq else "quantity"]
        values[f"position.{i}.sellable"] = position["closeable_amount" if jq else "sellable"]
    return values


def differences(aq, jq):
    return [{"field": k, "aq_present": k in aq, "jq_present": k in jq,
             "aq": aq.get(k), "jq": jq.get(k)}
            for k in sorted(aq.keys() | jq.keys())
            if k not in aq or k not in jq or aq[k] != jq[k]]


def compare(name, evidence):
    aq_path = evidence / "aq" / name / "events.jsonl"
    jq_path = evidence / "reference" / name / "events.jsonl"
    aq, jq = events(aq_path), events(jq_path)
    snapshots = [e for e in aq if e["kind"] == "snapshot"]
    comparisons = []
    terminal = []
    for reference in (e for e in jq if e["kind"] == "snapshot"):
        label = {"day_start": "initial", "auction_checkpoint": "opening_checkpoint"}.get(
            reference["label"], reference["label"])
        # Terminal offset is compared explicitly below, never normalized away.
        candidates = [e for e in snapshots if e["label"] == label and
                      (label == "strategy_end" or timestamp(e["at"]) == timestamp(reference["time"]))]
        assert len(candidates) == 1, (name, label, reference["time"])
        actual = candidates[0]
        left, right = project(actual, False), project(reference, True)
        record = {"label": label, "aq_seq": actual["seq"], "jq_seq": reference["seq"],
                  "aq_at": actual["at"], "jq_at": reference["time"],
                  "compared_fields": len(left.keys() | right.keys()),
                  "differences": differences(left, right)}
        (terminal if label == "strategy_end" else comparisons).append(record)
    fills = [e for e in aq if e["kind"] == "native_trade"]
    first = next((e for e in snapshots if e["trades"]), None)
    if name == "cancel_0928":
        assert fills == [] and first is None
        returned = next(e for e in aq if e["kind"] == "cancel_return")
        assert not returned["returned_none"] and returned["native_type"] == "rqalpha.model.order.Order"
        assert returned["order"]["native_status"] == "CANCELLED"
        assert any(e["kind"] == "native_order_event" and e["event"] == "ORDER_CANCELLATION_PASS"
                   and e["at"][11:16] == "09:28" for e in aq)
    else:
        assert len(fills) == 1 and first["at"][11:16] == "09:31"
        assert fills[0]["trade"]["at"][11:16] == "09:30"
        callback_exit = next(e for e in aq if e["kind"] == "callback_exit" and e["at"][11:16] == "09:30")
        assert callback_exit["seq"] < fills[0]["seq"] < first["seq"]
    pending = [e for e in snapshots if e["orders"] and e["orders"][0]["native_status"] == "ACTIVE"]
    assert pending
    assert all(e["open_order_ids"] == [] and len(e["native_open_order_ids"]) == 1 for e in pending)
    return {"name": name, "aq_events_sha256": sha(aq_path), "jq_events_sha256": sha(jq_path),
            "intraday_snapshots": len(comparisons),
            "intraday_fields": sum(r["compared_fields"] for r in comparisons),
            "intraday_differences": sum(len(r["differences"]) for r in comparisons),
            "comparisons": comparisons, "terminal": terminal,
            "native_trade_at": [e["trade"]["at"] for e in fills],
            "first_visible_trade_at": first["at"] if first else None,
            "native_state_preserved": True}


def review(evidence):
    for receipt_name in ("reference-receipts.json", "aq-receipts.json"):
        for name, receipt in json.loads((evidence / receipt_name).read_text()).items():
            domain = "reference" if receipt_name.startswith("reference") else "aq"
            for file, digest in receipt["files"].items():
                assert sha(evidence / domain / name / file) == digest, (name, file)
    baseline = events(evidence / "aq/baseline_original/events.jsonl")
    pending = [e for e in baseline if e["kind"] == "snapshot" and e["at"][11:16] <= "09:30"
               and e["orders"]]
    assert len(pending) == 4 and all(len(e["open_order_ids"]) == 1 for e in pending)
    failure = json.loads((evidence / "aq/baseline_cancel_probe/failure.json").read_text())
    assert failure["message"] == "SIX_CANCEL_INTENT_OUT_OF_SCOPE"
    results = [compare(name, evidence) for name in ("original", "keep_0920", "cancel_0928")]
    return {"schema": "aq.b12.observable-review.v1",
            "status": "PASS" if all(r["intraday_differences"] == 0 for r in results) else "DIFFERENCES",
            "baseline_open_query_differences": 4, "baseline_cancel": failure["message"],
            "cases": results,
            "comparison_scope": "saved minute strategy callbacks through end_of_day; selected order/cash/trade/position fields",
            "normalization": ["Asia/Shanghai applied to naive JQ timestamps", "exact Decimal numeric equality",
                              "platform IDs compared by order/trade counts and corresponding payloads"],
            "limitations": ["Independent 09:25 matching remains unproven; native fill timestamps stay 09:30.",
                            "Same AD observation product as original B12; not independent market verification.",
                            "JQ terminal clears daily orders/trades and advances sellable quantity; AQ retains native records.",
                            "Terminal callbacks differ by 1 ms; raw terminal differences remain in this report.",
                            "AQ strategy_status is an explicit query model, not an RQAlpha native status field.",
                            "AQ finished_at comes from native trade time; canceled_at is native event clock, not new native Order getters."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = review(args.evidence)
    payload = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
