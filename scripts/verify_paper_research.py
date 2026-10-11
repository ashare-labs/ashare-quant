"""All frozen research windows, one native day/process at a time; no retuning."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import atomic
from ashare_quant.paper.replay import advance, bind
from ashare_quant.paper.service import PaperService

REPO = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--owner-input-dir", required=True, type=Path)
    p.add_argument("--reference-dir", required=True, type=Path)
    p.add_argument(
        "--cash-dividend-policy",
        choices=("reject", "gross_zero_withholding_0900"),
        default="reject",
    )
    p.add_argument(
        "--execution-source",
        type=Path,
        help="Sealed AD v12 root; uses per-day admission/append, never legacy state fallback",
    )
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    risk = json.loads((REPO / "examples/daily-paper-research-risk.json").read_text())
    atomic(out / "risk.json", risk)
    manifest = REPO / "examples/daily_portfolio/manifest.json"
    scopes = json.loads(manifest.read_text())["windows"]
    results = []

    def run(case):
        window, strategy = case
        name = window["id"] + "-" + strategy
        service = PaperService(out / name / "store")
        if args.execution_source:
            from ashare_quant.paper.execution import bind as bind_execution
            from ashare_quant.paper.execution import ingest

            fixed = args.execution_source.resolve()
            contract = json.loads((fixed / "consumer.json").read_text())
            delivery = bind_execution(
                service,
                manifest,
                fixed / "research/binding.json",
                contract["research_sha"],
                fixed / "execution/binding.json",
                contract["execution_sha"],
                fixed / contract["wheel"],
                contract["wheel_sha"],
                contract["ad_commit"],
                strategy,
                window["id"],
                "ad-v12-" + name,
                accepted_assumptions=contract["accepted_assumptions"],
                accept_listing_continuity=contract["accept_listing_continuity"],
                cash_credit_policy=contract["cash_credit_policy"],
                consumer_contract=fixed / contract["consumer_contract"]
                if contract.get("consumer_contract")
                else None,
                consumer_contract_sha=contract.get("consumer_contract_sha"),
                risk=risk,
            )
            sid = delivery["session_id"]
        else:
            sid = bind(
                service,
                manifest,
                args.owner_input_dir / (window["id"] + "-model.json"),
                strategy,
                window["id"],
                "policy-v1-" + name,
                accept_model=True,
                risk=risk,
                cash_dividend_policy=args.cash_dividend_policy,
            )["session_id"]
        reference = json.loads((args.reference_dir / name / "result.json").read_text())
        accounts = {a["date"]: a for a in reference["accounts"]}
        decisions = {d["date"]: d for d in reference["decisions"]}
        item = {
            "name": name,
            "session_id": sid,
            "risk": risk,
            "days": [],
            "stopped": None,
            "differences": [],
            "refusals": [],
            "max_cash_delta": "0",
            "max_equity_delta": "0",
        }
        parent = None
        seen = set()
        for day in service.view(sid)["replay"]["days"]:
            try:
                if args.execution_source:
                    packet = ingest(service, sid, day, parent)
                    duplicate = ingest(service, sid, day, parent)
                    assert packet["packet_sha256"] == duplicate["packet_sha256"]
                r = advance(service, sid, day, parent)
            except DomainError as exc:
                assert service.status(sid)["session"]["head"] == parent
                item["stopped"] = {
                    "day": day,
                    "code": exc.code,
                    "message": str(exc),
                    "head_unchanged": True,
                }
                break
            n = r["native"]
            assert n["cursor"] == (len(item["days"]) + 1) * 4
            assert n["settlement_count"] == len(item["days"]) + 1
            assert all(e["at"].startswith(day) for e in n["events"])
            assert all(t["id"] not in seen for t in n["trades"])
            seen.update(t["id"] for t in n["trades"])
            actual, expected = n["account"], accounts[day]
            for a, b, label in [("total_cash", "cash", "cash"), ("nav", "equity", "equity")]:
                delta = abs(Decimal(actual[a]) - Decimal(expected[b]))
                item["max_" + label + "_delta"] = str(
                    max(delta, Decimal(item["max_" + label + "_delta"]))
                )
                if delta > Decimal("0.000001"):
                    item["differences"].append(
                        {"day": day, "field": label, "actual": actual[a], "reference": expected[b]}
                    )
            for security, pos in actual["positions"].items():
                ref = expected["positions"].get(
                    security, {"quantity": 0, "sellable": 0, "value": "0"}
                )
                if (
                    pos["quantity"] != ref["quantity"]
                    or pos["sellable"] != ref["sellable"]
                    or abs(Decimal(pos["market_value"]) - Decimal(ref["value"]))
                    > Decimal("0.000001")
                    or abs(
                        Decimal(pos.get("dividend_receivable", "0"))
                        - Decimal(ref.get("dividend_receivable", "0"))
                    )
                    > Decimal("0.000001")
                    or (
                        ref["quantity"]
                        and abs(Decimal(pos["average_cost"]) - Decimal(ref["average_price"]))
                        > Decimal("0.000001")
                    )
                ):
                    item["differences"].append(
                        {
                            "day": day,
                            "field": "position",
                            "security": security,
                            "actual": pos,
                            "reference": ref,
                        }
                    )
            receipt = r["strategy_receipt"]
            if receipt["scheduled"] != decisions[day]["rebalance"] or (
                receipt["scheduled"]
                and receipt["signal"]["targets"] != decisions[day]["signal"]["targets"]
            ):
                item["differences"].append({"day": day, "field": "signal"})
            columns = ("security", "side", "quantity", "price", "commission", "tax")

            def trade(t):
                return tuple(
                    t[k] if k in ("security", "side", "quantity") else Decimal(t[k])
                    for k in columns
                )

            if [trade(t) for t in n["trades"]] != [
                trade(t) for t in reference["trades"] if t["date"] == day
            ]:
                item["differences"].append({"day": day, "field": "trades"})
            item["days"].append(
                {
                    "day": day,
                    "head": r["checkpoint_sha256"],
                    "cursor": n["cursor"],
                    "trades": len(n["trades"]),
                    "cash": actual["total_cash"],
                    "equity": actual["nav"],
                    "action_checks": receipt["action_checks"],
                    "opening_reconciliation": n["opening_reconciliation"],
                }
            )
            item["refusals"].extend({"day": day, **v} for v in n["refusals"])
            parent = r["checkpoint_sha256"]
            atomic(out / name / "result.json", item)
            if len(item["days"]) % 10 == 0:
                print(name, day, len(item["days"]), "days", len(seen), "unique fills", flush=True)
        item["unique_fills"] = len(seen)
        item["complete_window"] = item["stopped"] is None
        if item["days"]:
            last = item["days"][-1]["day"]
            old = service.day(sid, last)
            frozen_parent = json.loads(old["decision"])["expected_parent"]
            assert advance(service, sid, last, frozen_parent) == service.report(sid, last)
        atomic(out / name / "result.json", item)
        print(
            name,
            "FINAL",
            len(item["days"]),
            "days",
            len(item["differences"]),
            "differences",
            item["stopped"],
            flush=True,
        )
        return item

    cases = [(w, s) for w in scopes for s in ("equal-weight", "momentum", "trend")]
    with ThreadPoolExecutor(max_workers=3) as workers:
        for item in workers.map(run, cases):
            results.append(item)
            atomic(
                out / "matrix.json",
                {"risk": risk, "continuous_paper_accepted": False, "cases": results},
            )
    if any(item["differences"] for item in results):
        raise SystemExit("Native/reference differences retained in matrix.json")
    for item in results:
        if not item["stopped"]:
            continue
        expected_ad_block = (
            bool(args.execution_source)
            and item["name"].startswith("w2024-")
            and item["stopped"]["code"] in {"PAPER_AD_NOT_ADMITTED", "PAPER_AD_ORDER_NOT_ADMITTED"}
        )
        expected_legacy_block = (
            not args.execution_source
            and args.cash_dividend_policy == "reject"
            and item["name"] == "w2022-equal-weight"
            and item["stopped"]["code"] == "PAPER_ACTION_TIMING_UNSUPPORTED"
        )
        if not expected_ad_block and not expected_legacy_block:
            raise SystemExit("Unexpected stopped case retained in matrix.json: " + item["name"])


if __name__ == "__main__":
    main()
