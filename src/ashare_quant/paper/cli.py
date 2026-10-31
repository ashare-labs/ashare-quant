"""Shared foreground CLI implementations for paper account operations and migrations."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import ashare_quant.portfolio.signals as signals
from ashare_quant.paper.calendar_successor import (
    bind as bind_successor,
)
from ashare_quant.paper.calendar_successor import (
    build_plan as build_successor_plan,
)
from ashare_quant.paper.calendar_successor import (
    current_authority,
)
from ashare_quant.paper.intraday import IntradayReader, config_for, deliver, now, prepare
from ashare_quant.paper.intraday_continuation import bind as bind_continuation
from ashare_quant.paper.intraday_contracts import REBALANCE_MODEL, IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_adapter import UNIVERSE, OwnerReader
from ashare_quant.paper.online_contracts import CalendarReplan, OnlineSession
from ashare_quant.paper.risk import profiles, require
from ashare_quant.paper.runtime_migration import migrate as migrate_runtime
from ashare_quant.paper.service import engine_pin
from ashare_quant.paper.settled_migration import migrate as migrate_settled
from ashare_quant.storage.artifacts import atomic, digest, regular, sha


def run_migration(description, migrate, *, binding_required=False, ensure_ascii=True):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", type=Path, required=True)
    names = ("state-sha", "from-sha", "to-sha")
    if binding_required:
        names += ("binding-sha",)
    for name in names:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review-sha")
    parser.add_argument("--reason")
    args = parser.parse_args()
    try:
        require((args.root / "state.json").is_file(), "PAPER_EXISTING_ACCOUNT_REQUIRED")
        arguments = {
            name.replace("-", "_"): getattr(args, name.replace("-", "_")) for name in names
        }
        result = migrate(
            OnlineService(args.root),
            **arguments,
            apply=args.apply,
            review_sha=args.review_sha,
            reason=args.reason,
        )
        print(json.dumps(dict(status="ok", result=result), ensure_ascii=ensure_ascii, indent=2))
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    status="blocked",
                    error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
                ),
                ensure_ascii=ensure_ascii,
                indent=2,
            )
        )
        raise SystemExit(2)


def runtime_migration_main():
    """Explicit pre-start runtime authorization; default operation only checks."""
    run_migration(runtime_migration_main.__doc__, migrate_runtime, binding_required=True)


def settled_migration_main():
    """Check or explicitly authorize a settled v3 ledger; never starts a trading day."""
    run_migration(settled_migration_main.__doc__, migrate_settled, ensure_ascii=False)


def online_main():
    """Explicit foreground operations; no daemon, scheduler, or broker integration."""
    parser = argparse.ArgumentParser(description=online_main.__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ad-python", required=True)
    parser.add_argument("--ad-source", type=Path, required=True)
    parser.add_argument("--ad-store", type=Path, required=True)
    parser.add_argument("--phase-store", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--account-id", required=True)
    sub.add_parser("status")
    prep = sub.add_parser("prepare")
    prep.add_argument("--capture", required=True)
    prep.add_argument("--admission", required=True)
    dlv = sub.add_parser("deliver")
    dlv.add_argument("--day", required=True)
    dlv.add_argument("--phase", required=True, choices=["opening", "closing"])
    dlv.add_argument("--capture", required=True)
    adv = sub.add_parser("advance")
    adv.add_argument("--day", required=True)
    adv.add_argument("--phase", required=True, choices=["opening", "closing"])
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


def intraday_main():
    """Explicit foreground commands for the separate intraday account."""
    p = argparse.ArgumentParser(description=intraday_main.__doc__)
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
                f"acceptance-rebalance-{plan.day:%Y%m%d}"
                if plan.model == REBALANCE_MODEL
                else f"intraday-{plan.day:%Y%m%d}-partial"
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


def continuation_main():
    """Build or bind a one-day holding continuation after the frozen close succeeds."""
    p = argparse.ArgumentParser(description=continuation_main.__doc__)
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
            result = bind_continuation(
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


def successor_main():
    """Build or explicitly bind the calendar-selected next holding session."""
    p = argparse.ArgumentParser(description=successor_main.__doc__)
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
            plan = build_successor_plan(service, reader, args.start_at)
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
            state = bind_successor(
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
