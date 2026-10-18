"""One explicit profile for local API, task worker, paper and complete recovery."""

import argparse
import json
import sys

from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.backup import create_backup, restore_backup, verify_backup
from ashare_quant.deployment.runtime import commands, doctor, initialize, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment")
    parser.add_argument("--profile", default="product")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "init", "commands", "api", "paper"):
        sub.add_parser(name)
    worker = sub.add_parser("task-worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--task-id")
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    backup.add_argument("--include-source-assets", action="store_true")
    for name in ("verify", "restore"):
        item = sub.add_parser(name)
        item.add_argument("backup")
        item.add_argument("--expected-sha256")
        if name == "restore":
            item.add_argument("destination")
    args = parser.parse_args(argv)
    try:
        if args.command == "restore":
            result = restore_backup(args.backup, args.destination, expected_sha256=args.expected_sha256)
        elif args.command == "verify":
            value = verify_backup(args.backup, expected_sha256=args.expected_sha256)
            result = {"verified": True, "scope": value["scope"], "files": len(value["files"]),
                      "profile": value["profile"], "runtime_verified": True}
        else:
            config = resolve(args.deployment, args.profile)
            if args.command in {"api", "paper", "task-worker"}:
                return run(config, args.command, once=getattr(args, "once", False),
                           task_id=getattr(args, "task_id", None))
            if args.command == "backup":
                result = create_backup(config, args.destination, include_source_assets=args.include_source_assets)
            else:
                result = {"doctor": doctor, "init": initialize, "commands": commands}[args.command](config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DomainError as exc:
        print(json.dumps({"error": exc.body()}, ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": "DEPLOYMENT_IO_ERROR", "message": str(exc)}}))
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
