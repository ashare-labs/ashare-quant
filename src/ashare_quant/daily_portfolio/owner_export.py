"""Consume AD's public DailyResearchView in its separate owner environment.

This is an AQ boundary codec, not a collector or data provider. The normalized
file is an immutable run input with the public API read receipts attached.
"""

import argparse
import hashlib
import inspect
import json
from decimal import Decimal
from pathlib import Path

from .inputs import digest


def export(binding, binding_sha256, window, *, accept_model=False):
    from ashare_data.daily_research import ROLE, DailyResearchView

    owner = DailyResearchView(binding, expected_binding_sha256=binding_sha256)
    descriptor = owner.descriptor()
    bundle = owner.read_window(window, role=ROLE)
    w, ids = bundle["window"], bundle["securities"]
    days = [d for d in w["sessions"] if w["start"] <= d <= w["end"]]
    rows = {(r["security"], r["date"]): dict(r) for r in bundle["bars"]}
    reads, history_hashes, missing = [], {}, []
    for day in days:
        history = {}
        for sid in ids:
            h = owner.history(
                window,
                sid,
                as_of=day + "T09:00:00+08:00",
                count=120,
                fields=("close",),
                adjustment=None,
            )
            history[sid] = [r["close"] for r in h["rows"]]
            reads.append(
                {
                    "operation": "history",
                    "date": day,
                    "security": sid,
                    "count": len(h["rows"]),
                    "first": h["rows"][0]["date"],
                    "last": h["rows"][-1]["date"],
                    "response_sha256": digest(h),
                    "report": h["report"],
                }
            )
            state = owner.state(
                window, sid, as_of=day + "T09:30:00+08:00", accept_model=accept_model
            )
            selected = state["model"] if accept_model else state["observed"]
            row = rows[sid, day]
            row.update({f: selected.get(f) for f in ("suspended", "upper_limit", "lower_limit")})
            row["state_origin"] = (
                "explicit_retrospective_owner_model" if accept_model else "observed"
            )
            row["state_evidence"] = state
            reads.append(
                {
                    "operation": "state",
                    "date": day,
                    "security": sid,
                    "response_sha256": digest(state),
                    "report": state["report"],
                }
            )
            if row["traded"]:
                for operation, clock, field in (
                    ("opening", "09:30:00", "open"),
                    ("closing", "15:30:00", "close"),
                ):
                    point = getattr(owner, operation)(
                        window, sid, as_of=day + "T" + clock + "+08:00"
                    )
                    if point[field] != row[field]:
                        raise ValueError("OWNER_POINT_BAR_DISAGREEMENT")
                    reads.append(
                        {
                            "operation": operation,
                            "date": day,
                            "security": sid,
                            "response_sha256": digest(point),
                            "report": point["report"],
                        }
                    )
        history_hashes[day] = digest(history)
    actions, coverage = [], {}
    for sid in ids:
        answer = owner.corporate_actions(window, sid, as_of=w["next_session"] + "T09:00:00+08:00")
        coverage[sid] = answer["coverage"]
        reads.append(
            {
                "operation": "corporate_actions",
                "security": sid,
                "response_sha256": digest(answer),
                "report": answer["report"],
            }
        )
    for event in bundle["actions"]:
        common = {k: event[k] for k in ("security", "ex_date", "record_date")}
        if Decimal(event["cash_per_share"]) > 0:
            pay = event.get("payment_date")
            if not pay:
                missing.append(
                    {
                        "field": "cash_payment_date",
                        "security": event["security"],
                        "date": event["ex_date"],
                    }
                )
            else:
                actions.append(
                    {
                        **common,
                        "kind": "cash_dividend",
                        "cash_per_share": event["cash_per_share"],
                        "pay_date": pay,
                        "source_event": event,
                    }
                )
        shares = Decimal(event["bonus_share_ratio"]) + Decimal(event["capitalization_share_ratio"])
        if shares:
            tradable = event.get("new_shares_tradable_date")
            credit = event.get("shares_credit_date")
            if not tradable or not credit:
                missing.append(
                    {
                        "field": "share_credit_or_tradable_date",
                        "security": event["security"],
                        "date": event["ex_date"],
                    }
                )
            else:
                actions.append(
                    {
                        **common,
                        "kind": "split",
                        "ratio": str(1 + shares),
                        "credit_date": credit,
                        "tradable_date": tradable,
                        "source_event": event,
                    }
                )
    producer = {
        **descriptor["producer"],
        "owner_api": "ashare_data.daily_research.DailyResearchView",
        "owner_module_sha256": hashlib.sha256(
            Path(inspect.getfile(DailyResearchView)).read_bytes()
        ).hexdigest(),
    }
    return {
        "schema": "ad-daily-portfolio.v1",
        "producer": producer,
        "real_market_data": True,
        "source_refs": [
            {"binding_path": str(Path(binding).resolve()), "binding_sha256": binding_sha256}
        ],
        "limitations": bundle["limitations"],
        "assumptions": bundle["assumptions"],
        "price_basis": "raw",
        "volume_unit": "share",
        "securities": ids,
        "calendar": w["sessions"],
        "daily": list(rows.values()),
        "actions": actions,
        "action_coverage": {
            "status": "source_covered" if not missing else "incomplete",
            "scope": "provider-scoped event table; not a complete certified issuer chain",
            "securities": coverage,
        },
        "additional_gaps": missing,
        "listing_coverage": "scoped_research_model",
        "listing_evidence": bundle["listing"],
        "state_mode": "retrospective_model" if accept_model else "strict_observed",
        "owner_history_sha256": history_hashes,
        "owner_api_reads": reads,
        "owner_descriptor": descriptor,
        "strict_preopen_state_verified": False,
        "economic_acceptance": "conditional_research_only",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binding", required=True, type=Path)
    p.add_argument("--binding-sha256", required=True)
    p.add_argument("--window", required=True)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--accept-owner-state-model", action="store_true")
    args = p.parse_args()
    from rqprobe.guards import install_network_guard

    install_network_guard()
    value = export(
        args.binding, args.binding_sha256, args.window, accept_model=args.accept_owner_state_model
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "state_mode": value["state_mode"],
                "additional_gaps": value["additional_gaps"],
            }
        )
    )


if __name__ == "__main__":
    main()
