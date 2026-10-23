"""Inspect existing paper accounts and frozen reports."""
import argparse
import json

from ashare_quant.paper.service import PaperService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sessions")
    for name in ("status", "view", "report"):
        command = commands.add_parser(name)
        command.add_argument("--session", required=True)
        if name == "report":
            command.add_argument("--day", required=True)
    commands.add_parser("serve").add_argument("--port", type=int, default=18818)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from ashare_quant.paper.api import create_app
        uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port)
        return
    service = PaperService(args.root)
    if args.command == "sessions":
        value = service.catalogue()
    elif args.command == "report":
        value = service.report(args.session, args.day)
    else:
        value = getattr(service, args.command)(args.session)
    print(json.dumps(value, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
