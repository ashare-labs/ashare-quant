"""Build or bind a one-day holding continuation after the frozen close succeeds."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from ashare_quant.m2_jobs.files import atomic, digest, regular, sha

from .intraday import IntradayReader
from .intraday_continuation import bind
from .intraday_contracts import IntradayPlan
from .online import OnlineService
from .risk import require
from .service import engine_pin


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("root", "ad-source", "daily-store", "phase-store"):
        p.add_argument("--" + key, type=Path, required=True)
    for key in ("ad-python", "engine-pin-sha", "origin-engine-pin-sha"):
        p.add_argument("--" + key, required=True)
    sub = p.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-plan")
    build.add_argument("--start-at", required=True)
    build.add_argument("--output", type=Path, required=True)
    binding = sub.add_parser("bind")
    binding.add_argument("--plan", type=Path, required=True)
    binding.add_argument("--plan-sha", required=True)
    binding.add_argument("--state-sha", required=True)
    args = p.parse_args()
    try:
        require(
            (args.root / "state.json").is_file(), "PAPER_CONTINUATION_EXISTING_ACCOUNT_REQUIRED"
        )
        require(digest(engine_pin()) == args.engine_pin_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
        service = OnlineService(args.root)
        state, _ = service._load(verify=False)
        require(
            digest(state["engine_pin"]) == args.origin_engine_pin_sha,
            "PAPER_CONTINUATION_ORIGIN_RUNTIME",
        )
        reader = IntradayReader(args.ad_python, args.ad_source, args.daily_store, args.phase_store)
        if args.command == "bind":
            raw = regular(args.plan)
            require(sha(raw) == args.plan_sha, "PAPER_CONTINUATION_PLAN_CHANGED")
            result = bind(
                service,
                reader,
                IntradayPlan.model_validate_json(raw),
                args.state_sha,
                args.engine_pin_sha,
                args.origin_engine_pin_sha,
            )
        else:
            day = state["last_day"]
            require(
                day is not None
                and set(state["days"]) == {day}
                and state["days"][day]["status"] == "committed",
                "PAPER_CONTINUATION_CLOSE_REQUIRED",
            )
            service._report(state, day, "closing")
            row = state["days"][day]
            start = datetime.fromisoformat(args.start_at)
            plan = IntradayPlan.model_validate(
                {
                    **row["decision"]["strategy_receipt"]["intraday_plan"],
                    "day": row["decision"]["next_open"],
                    "start_at": start,
                    "intent_deadline": start + timedelta(minutes=5),
                    "execution_deadline": start + timedelta(minutes=15),
                    "daily_capture": row["close"]["data_manifest_sha256"],
                }
            )
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
                account_unchanged=True,
                activated=False,
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
