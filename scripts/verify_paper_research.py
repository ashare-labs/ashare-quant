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
        sid = bind(
            service,
            manifest,
            args.owner_input_dir / (window["id"] + "-model.json"),
            strategy,
            window["id"],
            "policy-v1-" + name,
            accept_model=True,
            risk=risk,
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


if __name__ == "__main__":
    main()
