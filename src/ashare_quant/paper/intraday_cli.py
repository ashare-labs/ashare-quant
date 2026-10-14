"""Explicit foreground commands for the separate intraday account."""

import argparse
import json
from pathlib import Path

from ashare_quant.m2_jobs.files import digest, regular

from .intraday import IntradayReader, config_for, deliver, now, prepare
from .intraday_contracts import IntradayPlan
from .online import OnlineService
from .risk import require
from .service import engine_pin


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "plan", "ad-source", "daily-store", "phase-store"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--ad-python", required=True)
    p.add_argument("--engine-pin-sha", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    for name in ("prepare", "deliver", "close"):
        sub.add_parser(name).add_argument("--capture", required=True)
    sub.add_parser("advance").add_argument("--phase", choices=["opening", "closing"], required=True)
    sub.add_parser("resume").add_argument("--reason", required=True)
    args = p.parse_args()
    try:
        require(digest(engine_pin()) == args.engine_pin_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
        plan = IntradayPlan.model_validate_json(regular(args.plan))
        reader = IntradayReader(args.ad_python, args.ad_source, args.daily_store, args.phase_store)
        service = OnlineService(args.root)
        if args.command == "init":
            owned = reader.reviewed_plan()
            require(
                owned["first_window"][0] <= str(plan.day) <= owned["first_window"][1]
                and owned["action_baseline_sha256"] == plan.action_baseline_sha256
                and owned["admission_reader_sha256"] == plan.admission_reader_sha256,
                "PAPER_INTRADAY_OWNER_PLAN",
            )
            require(
                now() < plan.start_at or service.path.exists(),
                "PAPER_INTRADAY_START_ALREADY_PASSED",
            )
            account = (
                f"intraday-{plan.day:%Y%m%d}-partial"
                if plan.remainder_policy
                else "intraday-20261014-independent"
            )
            result = service.create(config_for(plan, account))
        elif args.command == "prepare":
            result = prepare(service, reader, plan, args.capture)
        elif args.command == "deliver":
            result = deliver(service, reader, plan, args.capture)
        elif args.command == "advance":
            result = service.advance(str(plan.day), args.phase)
        elif args.command == "resume":
            result = service.resume(args.reason)
        elif args.command == "close":
            result = service.deliver_observed(reader, str(plan.day), "closing", args.capture)
        else:
            result = service.status()
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
