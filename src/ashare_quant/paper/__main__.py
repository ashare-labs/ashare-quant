"""Explicit local operations. Does not install timers, fetch data or invoke a broker."""

import argparse
import json
from contextlib import nullcontext

from .service import PaperService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--input", required=True)
    replay = commands.add_parser("bind-replay")
    replay.add_argument("--manifest", required=True)
    replay.add_argument("--input", required=True)
    replay.add_argument("--strategy", choices=("equal-weight", "momentum", "trend"), required=True)
    replay.add_argument("--window", required=True)
    replay.add_argument("--account", required=True)
    replay.add_argument("--risk")
    replay.add_argument("--accept-owner-state-model", action="store_true")
    replay.add_argument(
        "--cash-dividend-policy",
        choices=("reject", "gross_zero_withholding_0900"),
        default="reject",
    )
    advance = commands.add_parser("advance")
    advance.add_argument("--session", required=True)
    advance.add_argument("--day", required=True)
    advance.add_argument("--expected-parent")
    execution = commands.add_parser("bind-execution")
    for field in (
        "manifest",
        "research-binding",
        "research-sha",
        "execution-binding",
        "execution-sha",
        "ad-wheel",
        "wheel-sha",
        "ad-commit",
        "strategy",
        "window",
        "account",
        "risk",
        "cash-credit-policy",
    ):
        execution.add_argument("--" + field, required=True)
    execution.add_argument("--accept-assumption", action="append", default=[])
    execution.add_argument("--accept-listing-continuity", action="store_true")
    execution.add_argument("--consumer-contract")
    execution.add_argument("--consumer-contract-sha")
    receive = commands.add_parser("ingest")
    receive.add_argument("--session", required=True)
    receive.add_argument("--day", required=True)
    receive.add_argument("--expected-parent")
    for name in ("prepare", "seal-close", "status", "work", "report", "pause", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("--session", required=True)
        if name in {"prepare", "seal-close"}:
            sub.add_argument("--input", required=True)
        if name in {"work", "report"}:
            sub.add_argument("--day", required=True)
        if name in {"pause", "resume"}:
            sub.add_argument("--expected-parent")
            sub.add_argument("--reason", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8808)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port)
        return
    service = PaperService(args.root)
    if args.command == "bind-execution":
        from .execution import bind

        with open(args.risk) as stream:
            risk = json.load(stream)
        result = bind(
            service,
            args.manifest,
            args.research_binding,
            args.research_sha,
            args.execution_binding,
            args.execution_sha,
            args.ad_wheel,
            args.wheel_sha,
            args.ad_commit,
            args.strategy,
            args.window,
            args.account,
            accepted_assumptions=args.accept_assumption,
            accept_listing_continuity=args.accept_listing_continuity,
            cash_credit_policy=args.cash_credit_policy,
            risk=risk,
            consumer_contract=args.consumer_contract,
            consumer_contract_sha=args.consumer_contract_sha,
        )
    elif args.command == "ingest":
        from .execution import ingest

        result = ingest(service, args.session, args.day, args.expected_parent)
    elif args.command == "bind-replay":
        from .replay import bind

        with open(args.risk) if args.risk else nullcontext() as stream:
            risk = json.load(stream) if stream else None
        result = bind(
            service,
            args.manifest,
            args.input,
            args.strategy,
            args.window,
            args.account,
            accept_model=args.accept_owner_state_model,
            risk=risk,
            cash_dividend_policy=args.cash_dividend_policy,
        )
    elif args.command == "advance":
        from .replay import advance

        result = advance(service, args.session, args.day, args.expected_parent)
    elif args.command == "create":
        with open(args.input) as stream:
            result = service.create(json.load(stream))
    elif args.command in {"prepare", "seal-close"}:
        with open(args.input) as stream:
            fn = service.prepare if args.command == "prepare" else service.seal_close
            result = fn(args.session, json.load(stream))
    elif args.command in {"pause", "resume"}:
        result = service.control(args.session, args.command, args.expected_parent, args.reason)
    elif args.command in {"work", "report"}:
        result = getattr(service, args.command)(args.session, args.day)
    else:
        result = service.status(args.session)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
