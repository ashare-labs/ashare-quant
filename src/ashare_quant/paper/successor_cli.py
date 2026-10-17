"""Build or explicitly bind the calendar-selected next holding session."""

import argparse
import json
from pathlib import Path

from ashare_quant.paper.calendar_successor import bind, build_plan, current_authority
from ashare_quant.paper.intraday import IntradayReader
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.risk import require
from ashare_quant.paper.service import engine_pin
from ashare_quant.storage.artifacts import atomic, digest, regular, sha


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("root", "ad-source", "daily-store", "phase-store"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--ad-python", required=True)
    p.add_argument("--runtime-sha", required=True)
    p.add_argument("--from-runtime-sha", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build-plan")
    b.add_argument("--start-at", required=True)
    b.add_argument("--output", type=Path, required=True)
    b = sub.add_parser("bind")
    for key in ("plan-sha", "state-sha", "review-sha"):
        b.add_argument("--" + key, required=True)
    b.add_argument("--plan", type=Path, required=True)
    args = p.parse_args()
    try:
        require(
            (args.root / "state.json").is_file(), "PAPER_CONTINUATION_EXISTING_ACCOUNT_REQUIRED"
        )
        require(digest(engine_pin()) == args.runtime_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
        service = OnlineService(args.root)
        reader = IntradayReader(args.ad_python, args.ad_source, args.daily_store, args.phase_store)
        if args.command == "build-plan":
            state, _ = service._load(verify=False)
            require(
                digest(current_authority(state)) == args.from_runtime_sha,
                "PAPER_SUCCESSOR_FROM_RUNTIME",
            )
            plan = build_plan(service, reader, args.start_at)
            if args.output.exists():
                require(
                    IntradayPlan.model_validate_json(regular(args.output)) == plan,
                    "PAPER_CONTINUATION_PLAN_CHANGED",
                )
            else:
                atomic(args.output, plan)
            result = dict(
                plan_path=str(args.output),
                plan_sha256=sha(regular(args.output)),
                origin_state_sha256=sha(regular(service.path)),
                parent_checkpoint=state["head"],
                plan=plan.model_dump(mode="json"),
                activated=False,
            )
        else:
            raw = regular(args.plan)
            require(sha(raw) == args.plan_sha, "PAPER_CONTINUATION_PLAN_CHANGED")
            state = bind(
                service,
                reader,
                IntradayPlan.model_validate_json(raw),
                state_sha=args.state_sha,
                from_runtime_sha=args.from_runtime_sha,
                runtime_sha=args.runtime_sha,
                review_sha=args.review_sha,
            )
            result = dict(
                state_sha256=sha(regular(service.path)),
                binding_sha256=digest(state["calendar_successor"]),
                parent_checkpoint=state["head"],
                plan=state["calendar_successor"]["plan"],
                activated=True,
            )
        print(json.dumps(dict(status="ok", result=result), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    status="blocked",
                    error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
