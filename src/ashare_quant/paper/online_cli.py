"""Explicit foreground operations; no daemon, scheduler, or broker integration."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ashare_quant.daily_portfolio import signals
from ashare_quant.m2_jobs.files import atomic, digest, regular, sha

from .online import OnlineService
from .online_adapter import UNIVERSE, OwnerReader
from .online_contracts import CalendarReplan, OnlineSession
from .risk import profiles, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ad-python", required=True)
    parser.add_argument("--ad-source", type=Path, required=True)
    parser.add_argument("--ad-store", type=Path, required=True)
    parser.add_argument("--phase-store", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--account-id", required=True)
    sub.add_parser("status")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--capture", required=True)
    prepare.add_argument("--admission", required=True)
    deliver = sub.add_parser("deliver")
    deliver.add_argument("--day", required=True)
    deliver.add_argument("--phase", required=True, choices=["opening", "closing"])
    deliver.add_argument("--capture", required=True)
    advance = sub.add_parser("advance")
    advance.add_argument("--day", required=True)
    advance.add_argument("--phase", required=True, choices=["opening", "closing"])
    sub.add_parser("resume").add_argument("--reason", required=True)
    migrate = sub.add_parser("migrate-empty")
    migrate.add_argument("--state-sha", required=True)
    migrate.add_argument("--reason", required=True)
    migrate.add_argument("--plan", type=Path, help="Explicit reviewed finite calendar replan JSON")
    args = parser.parse_args()
    reader = OwnerReader(args.ad_python, args.ad_source, args.ad_store, args.phase_store)
    service = OnlineService(args.root)
    started = datetime.now(timezone.utc).isoformat()
    try:
        if args.command == "init":
            result = service.create(
                OnlineSession(
                    account_id=args.account_id,
                    strategy_version=sha(regular(Path(signals.__file__))),
                    scenario_manifest_sha256=digest(
                        {
                            "scope": UNIVERSE,
                            "first_window": ["2026-10-13", "2026-10-14"],
                            "strategy_id": "equal-weight",
                            "model": "prior-close-standing-intent-observed-open.v1",
                        }
                    ),
                    initial_cash="1000000",
                    universe=UNIVERSE,
                    risk=profiles()["target_then_native_cash_v1"],
                    admission_reader_sha256=reader.pin(),
                )
            )
        elif args.command == "migrate-empty":
            plan = CalendarReplan.model_validate_json(regular(args.plan)) if args.plan else None
            if plan is not None:
                owned = reader.reviewed_plan()
                supplied = plan.model_dump(mode="json")
                require(
                    all(supplied[k] == value for k, value in owned.items()),
                    "PAPER_REPLAN_OWNER_SCOPE",
                )
            result = service.migrate_empty(
                reader.pin(),
                args.state_sha,
                args.reason,
                plan=plan,
            )
        elif args.command == "status":
            if not service.path.exists():
                result = dict(
                    status="not_initialized", latest_account=None, continuous_paper_accepted=False
                )
            else:
                result = service.status()
                result["owner_reader_matches"] = (
                    result["config"]["admission_reader_sha256"] == reader.pin()
                )
        elif args.command == "prepare":
            result = service.prepare_observed(reader, args.capture, args.admission)
        elif args.command == "deliver":
            result = service.deliver_observed(reader, args.day, args.phase, args.capture)
        elif args.command == "advance":
            _, config = service._load()
            require(config.admission_reader_sha256 == reader.pin(), "PAPER_OWNER_READER_CHANGED")
            result = service.advance(args.day, args.phase)
        else:
            result = service.resume(args.reason)
        output = dict(status="ok", command=args.command, result=result)
        code = 0
    except Exception as exc:
        output = dict(
            status="blocked",
            command=args.command,
            error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
        )
        code = 2
    output.update(started_at=started, finished_at=datetime.now(timezone.utc).isoformat())
    if args.command != "status":
        directory = args.root / "operations"
        directory.mkdir(exist_ok=True)
        atomic(directory / (uuid4().hex + ".json"), output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
