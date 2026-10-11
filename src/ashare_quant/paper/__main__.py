"""Explicit local operations. Does not install timers, fetch data or invoke a broker."""

import argparse
import json

from .service import PaperService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--input", required=True)
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
    if args.command == "create":
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
