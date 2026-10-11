"""Isolated, offline AD v12 public API reader. Never imports native accounting."""

import hashlib
import json
import sys
from pathlib import Path

from rqprobe.guards import install_network_guard

install_network_guard()


def receipt(operation, security, value):
    return {
        "operation": operation,
        "security": security,
        "response_sha256": hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "report": value["report"],
        "row_dates": [r["date"] for r in value.get("rows", [])],
    }


def read(request):
    from ashare_data import DailyExecutionView, DailyResearchView
    from ashare_data.daily_research import ROLE

    root = Path(request["root"])
    spec = request["spec"]
    research = DailyResearchView(
        root / "research/binding.json", expected_binding_sha256=spec["research_sha256"]
    )
    owner = DailyExecutionView(
        research, root / "execution/binding.json", expected_binding_sha256=spec["execution_sha256"]
    )
    window = spec["window"]
    opts = {
        "accepted_assumptions": spec["accepted_assumptions"],
        "cash_credit_policy": spec["cash_credit_policy"],
    }
    admission = owner.preflight(window, purpose="retrospective_simulation", **opts)
    base = research.read_window(window, role=ROLE)
    descriptor = owner.descriptor()
    if request["operation"] == "describe":
        return {
            "descriptor": descriptor,
            "admission": admission,
            "window": base["window"],
            "securities": base["securities"],
            "listing": base["listing"],
            "limitations": base["limitations"],
            "source_role": "incrementally_delivered_retrospective_simulation",
        }
    if not admission["ready"]:
        return {"blocked": admission}
    engine = owner.read_window(window, purpose="retrospective_simulation", **opts)
    if not engine["report"]["simulation_admitted"]:
        raise ValueError("AD_SIMULATION_NOT_ADMITTED")
    day = request["day"]
    history, rows, reads = {}, {}, []
    listings = {r["security"]: r for r in base["listing"]}
    for security in base["securities"]:
        h = owner.history(
            window,
            security,
            as_of=day + "T09:00:00+08:00",
            count=120,
            fields=("close",),
            adjustment=None,
        )
        sessions = base["window"]["sessions"]
        index = sessions.index(day)
        if [r["date"] for r in h["rows"]] != sessions[index - 120 : index]:
            raise ValueError("AD_HISTORY_WATERMARK_OR_COVERAGE")
        state = owner.state(window, security, as_of=day + "T09:00:00+08:00", **opts)
        opening = research.opening(window, security, as_of=day + "T09:30:00+08:00")
        closing = research.closing(window, security, as_of=day + "T15:30:00+08:00")
        bar = owner.history(
            window,
            security,
            as_of=day + "T15:30:00+08:00",
            count=1,
            fields=("open", "high", "low", "close", "volume"),
            adjustment=None,
        )
        current = bar["rows"][0]
        if (
            current["date"] != day
            or current["open"] != opening["open"]
            or current["close"] != closing["close"]
            or any(r["date"] >= day for r in h["rows"])
        ):
            raise ValueError("AD_POINT_HISTORY_SCOPE_MISMATCH")
        history[security] = [r["close"] for r in h["rows"]]
        listing = listings[security]
        if not (
            spec["accept_listing_continuity"]
            and listing["listing_date"] <= day
            and (listing["delisting_date"] is None or day < listing["delisting_date"])
        ):
            raise ValueError("AD_LISTING_SCOPE_UNADMITTED")
        facts = state["row"]
        if any(
            facts[f]["value"] is None for f in ("is_st", "suspended", "upper_limit", "lower_limit")
        ):
            raise ValueError("AD_REQUIRED_STATE_UNAVAILABLE")
        rows[security] = {
            **current,
            **{f: facts[f]["value"] for f in ("is_st", "suspended", "upper_limit", "lower_limit")},
            "eligible": True,
            "eligibility_evidence": {"listing": listing, "assumption": "scoped_listing_continuity"},
            "execution_state": state,
            "history_last_date": h["rows"][-1]["date"],
        }
        reads.extend(
            receipt(name, security, response)
            for name, response in (
                ("history", h),
                ("state", state),
                ("opening", opening),
                ("closing", closing),
                ("valuation_bar", bar),
            )
        )
    actions = []
    for a in engine["actions"]:
        # Source announcement date is distinct from a proven historical delivery
        # clock. This next-day visibility rule remains an explicit research model.
        if a["announcement_date"] >= day:
            continue
        if (
            a["type"] != "cash_dividend"
            or a["bonus_share_ratio"] != "0"
            or a["capitalization_share_ratio"] != "0"
        ):
            raise ValueError("AD_NONCASH_ACTION_UNSUPPORTED")
        actions.append(
            {
                "kind": "cash_dividend",
                "security": a["security"],
                "record_date": a["record_date"],
                "ex_date": a["ex_date"],
                "pay_date": a["payment_date"],
                "cash_per_share": a["cash_per_share"],
                "source_event": a,
            }
        )
        cash = owner.cash_state(
            window, a["security"], a["ex_date"], as_of=day + "T09:00:00+08:00", **opts
        )
        reads.append({**receipt("cash_state", a["security"], cash), "cash_state": cash})
    return {
        "day": day,
        "history": history,
        "rows": rows,
        "actions": actions,
        "admission": admission,
        "report": engine["report"],
        "action_coverage": engine["action_coverage"],
        "reads": reads,
    }


def main():
    try:
        value = read(json.load(sys.stdin))
        print(json.dumps({"result": value}, ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {"error": {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
