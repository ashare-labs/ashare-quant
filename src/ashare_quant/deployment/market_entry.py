"""Unified deployment entry with immutable markets and independently retained accounts."""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from ashare_data.core.model import DataError
from ashare_data.product import LocalMinuteProduct
from ashare_data.product.daily import LocalDailyProduct

from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.account_bridge import AccountReader, mount


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "command",
        choices=[
            "list",
            "show",
            "library",
            "daily",
            "minute",
            "minute-sources",
            "accounts",
            "account",
            "doctor",
            "commands",
            "api",
            "worker",
            "web",
            "market",
            "financial",
            "financial-archive",
            "factor",
        ],
    )
    args, remaining = parser.parse_known_args(argv)
    root = args.root.resolve()
    catalog = json.loads((root / "catalog.json").read_bytes())
    deployment = root / "deployment.json"
    config = resolve(deployment, "product")
    reader = AccountReader(root, catalog["account_reader"])
    if args.command in {"financial", "financial-archive", "factor"}:
        return subprocess.run(
            [
                str(root / catalog["account_reader"]["python"]),
                "-I",
                "-B",
                str(root / catalog["account_reader"]["entry"]),
                args.command,
                *remaining,
            ],
            check=False,
        ).returncode
    if args.command == "accounts":
        emit(reader.read())
        return 0
    if args.command == "account":
        p = argparse.ArgumentParser()
        p.add_argument("account_id")
        emit(reader.read(p.parse_args(remaining).account_id))
        return 0
    if args.command == "list":
        emit({"deployment": str(deployment), "datasets": catalog["datasets"]})
        return 0
    if args.command == "show":
        p = argparse.ArgumentParser()
        p.add_argument("id")
        selected = p.parse_args(remaining).id
        emit(next(item for item in catalog["datasets"] if item["id"] == selected))
        return 0
    if args.command == "market":
        from ashare_data.product.configuration import resolve as owner_resolve
        from ashare_data.product.release_cli import main as release_main

        selected = owner_resolve(deployment)["market_release"]
        return release_main(
            ["--root", selected["root"], "--release-id", str(selected["release_id"]), *remaining]
        )
    if args.command in {"library", "daily"}:
        owner = LocalDailyProduct.from_deployment(deployment)
        p = argparse.ArgumentParser()
        if args.command == "library":
            p.add_argument("--query", default="")
            p.add_argument("--limit", type=int, default=10)
            a = p.parse_args(remaining)
            emit(owner.catalogue(query=a.query, limit=a.limit))
        else:
            p.add_argument("--security", required=True)
            p.add_argument("--start", required=True)
            p.add_argument("--end", required=True)
            p.add_argument("--source", default="auto")
            a = p.parse_args(remaining)
            emit(owner.prices([a.security], a.start, a.end, source=a.source))
        return 0
    if args.command in {"minute", "minute-sources"}:
        owner = LocalMinuteProduct(deployment)
        if args.command == "minute-sources":
            emit(owner.sources())
            return 0
        p = argparse.ArgumentParser()
        p.add_argument("--security", required=True)
        p.add_argument("--source", default="auto")
        p.add_argument("--day")
        p.add_argument("--start")
        p.add_argument("--end")
        a = p.parse_args(remaining)
        if a.day:
            if a.start or a.end:
                p.error("Use --day or --start/--end")
            day = date.fromisoformat(a.day).isoformat()
            start, end = day + "T00:00:00+08:00", day + "T23:59:00+08:00"
        else:
            if not a.start or not a.end:
                p.error("Use --day or --start/--end")
            start, end = a.start, a.end
        emit(owner.prices(a.security, start, end, source=a.source))
        return 0
    if args.command in {"doctor", "commands"}:
        from ashare_quant.deployment.runtime import commands, doctor

        value = doctor(config) if args.command == "doctor" else commands(config)
        if args.command == "doctor":
            from ashare_data.product.configuration import resolve as owner_resolve
            from ashare_data.product.market_release import open_release

            try:
                value["market_release"] = open_release(owner_resolve(deployment)).receipt()
                value["registered_accounts"] = reader.read()
            except (DataError, DomainError, OSError, ValueError) as exc:
                value["ready"] = False
                value["market_or_account_error"] = str(exc)
        emit(value)
        return 0
    if args.command == "api":
        import uvicorn

        from ashare_quant.api import create_app
        from ashare_quant.deployment.runtime import deployment_lock, require_initialized
        from ashare_quant.tasks.executor import Configuration

        require_initialized(config)
        with deployment_lock(config):
            app = create_app(
                str(config.control_db),
                daily_tasks_configuration=Configuration(config.owner_store_root),
                product_configuration=config,
            )
            mount(app, reader)
            uvicorn.run(app, host="127.0.0.1", port=config.api_port)
        return 0
    if args.command == "worker":
        from ashare_quant.deployment.runtime import run

        return run(config, "task-worker")
    if args.command == "web":
        web = Path(catalog["code"]["web"]["path"])
        head = subprocess.check_output(
            ["git", "-C", str(web), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != catalog["code"]["web"]["commit"]:
            raise DomainError("WEB_RUNTIME_CHANGED", "Web source differs from its deployment pin")
        environment = dict(
            os.environ,
            ASHARE_API_URL=f"http://127.0.0.1:{config.api_port}",
            ASHARE_PAPER_URL=f"http://127.0.0.1:{config.paper_port}",
            FRONTEND_PORT=str(config.web_port),
        )
        os.execvpe("npm", ["npm", "--prefix", str(web), "run", "dev"], environment)
    raise AssertionError("Unsupported command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as exc:
        emit({"error": exc.as_dict()})
        sys.exit(2)
    except DomainError as exc:
        emit({"error": exc.body()})
        sys.exit(2)
