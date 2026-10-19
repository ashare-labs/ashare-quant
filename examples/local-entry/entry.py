"""Local entry to registered, existing data; no collection, migration or cleanup commands."""

import argparse
import csv
import hashlib
import json
import os
import subprocess
from datetime import date
from pathlib import Path

from ashare_data import DataError

ROOT = Path(__file__).resolve().parent


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def checked(item, key="path", hash_key="sha256"):
    path = Path(item[key])
    expected = item.get(hash_key)
    if expected and hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != expected:
        raise ValueError(
            "Registered content changed; review the catalogue pin before use: " + str(path)
        )
    return path


def main():
    parser = argparse.ArgumentParser(
        description="统一本机数据入口；数据留在原位，查询不采集、不迁移、不删除。"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出全部数据资产、真实位置、状态和限制")
    show = sub.add_parser("show", help="显示一个资产的来源和使用方式")
    show.add_argument("id")
    library = sub.add_parser("library", help="搜索正式日线库证券")
    library.add_argument("--query", default="")
    library.add_argument("--limit", type=int, default=10)
    daily = sub.add_parser("daily", help="只读正式库原始日线")
    daily.add_argument("--security", required=True)
    daily.add_argument("--start", required=True)
    daily.add_argument("--end", required=True)
    daily.add_argument("--source", default="auto")
    sub.add_parser("minute-sources", help="列出已登记分钟来源、覆盖及证据；默认 TDX")
    minute = sub.add_parser("minute", help="只读已登记分钟源；HF 需显式 --source neigezhu")
    minute.add_argument("--security", required=True)
    minute.add_argument("--source", default="tdx")
    minute.add_argument("--day")
    minute.add_argument("--start")
    minute.add_argument("--end")
    financial = sub.add_parser("financial", help="查询官方公告有界财务包，逐个核验来源文件")
    financial.add_argument("--security", required=True)
    financial.add_argument("--field", required=True)
    financial.add_argument("--as-of", required=True)
    financial.add_argument("--period")
    financial.add_argument("--basis", choices=["YTD", "quarter", "TTM"], default="YTD")
    financial.add_argument(
        "--mode",
        choices=["reported_date_next_day_incomplete_archive", "strict"],
        default="reported_date_next_day_incomplete_archive",
    )
    legacy = sub.add_parser(
        "financial-archive", help="读取旧财务抓取表，保留其 historical_pit=false 语义"
    )
    legacy.add_argument("--code", required=True)
    factor = sub.add_parser("factor", help="读取固定版本 Alpha191 结果及同格状态")
    factor.add_argument("--security", required=True)
    factor.add_argument("--day", required=True)
    factor.add_argument("--name", required=True)
    factor.add_argument("--variant", choices=["public", "explicit"], default="public")
    for command in ["doctor", "commands", "api", "worker", "web"]:
        sub.add_parser(command)
    args = parser.parse_args()
    catalog = json.loads((ROOT / "catalog.json").read_text())
    assets = {item["id"]: item for item in catalog["datasets"]}
    if args.command == "list":
        emit(
            {
                "entry_root": catalog["entry_root"],
                "deployment": catalog["deployment"],
                "datasets": [
                    {
                        key: item[key]
                        for key in ["id", "label", "status", "path", "paths", "access", "coverage"]
                        if key in item
                    }
                    for item in catalog["datasets"]
                ],
            }
        )
        return 0
    if args.command == "show":
        emit(assets[args.id])
        return 0
    if args.command in ["doctor", "commands", "api", "worker"]:
        from ashare_quant.deployment.__main__ import main as deployment_main

        return deployment_main(
            [
                "--deployment",
                str(ROOT / "deployment.json"),
                "task-worker" if args.command == "worker" else args.command,
            ]
        )
    if args.command == "web":
        config = json.loads((ROOT / "deployment.json").read_text())["profiles"]["product"]
        web = Path(catalog["code"]["web"]["path"])
        head = subprocess.check_output(
            ["git", "-C", str(web), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != catalog["code"]["web"]["commit"]:
            raise ValueError("Web main changed; recheck the package/contract pairing before launch")
        environment = dict(
            os.environ,
            ASHARE_API_URL=f"http://127.0.0.1:{config['api_port']}",
            ASHARE_PAPER_URL=f"http://127.0.0.1:{config['paper_port']}",
            FRONTEND_PORT=str(config["web_port"]),
        )
        os.execvpe("npm", ["npm", "--prefix", str(web), "run", "dev"], environment)
    if args.command in ["library", "daily"]:
        from ashare_data.product.daily import LocalDailyProduct

        owner = LocalDailyProduct.from_deployment(ROOT / "deployment.json")
        result = (
            owner.catalogue(query=args.query, limit=args.limit)
            if args.command == "library"
            else owner.prices([args.security], args.start, args.end, source=args.source)
        )
        emit(result)
        return 0
    if args.command in ["minute", "minute-sources"]:
        from ashare_data.product import LocalMinuteProduct

        owner = LocalMinuteProduct(ROOT / "deployment.json")
        if args.command == "minute-sources":
            emit(owner.sources())
            return 0
        if args.day is not None:
            if args.start is not None or args.end is not None:
                parser.error("--day cannot be combined with --start/--end")
            day = date.fromisoformat(args.day).isoformat()
            start, end = day + "T00:00:00+08:00", day + "T23:59:00+08:00"
        else:
            if args.start is None or args.end is None:
                parser.error("Use --day or both --start and --end with UTC offsets")
            start, end = args.start, args.end
        result = owner.prices(args.security, start, end, source=args.source)
        if args.source == "tdx":
            if not result["rows"]:
                raise ValueError(
                    "No registered TDX sample for this security/day; no fallback to a download"
                )
            binding = result["binding"]
            emit(
                {
                    "dataset": "minute-tdx",
                    "source_path": binding["path"],
                    "sha256": binding["content"]["sha256"],
                    "historical_pit": False,
                    "strict_execution_admitted": False,
                    "limits": binding["limits"],
                    "rows": result["rows"],
                }
            )
        else:
            emit(result)
        return 0
    if args.command == "financial":
        from ashare_data.financials.pit import FinancialPIT

        item = assets["financial-official"]
        path = checked(item)
        descriptor = checked(item, "reader_path", "reader_sha256")
        original = json.loads(path.read_text())
        adapted = json.loads(descriptor.read_text())
        if original["schema"] != "ad.f1-small-pack.v1" or adapted != {
            **original,
            "schema": "ad.financial-ledger.v1",
        }:
            raise ValueError(
                "Financial adapter must only change the documented schema discriminator"
            )
        owner = FinancialPIT.from_file(
            descriptor, evidence_root=path.parent, expected_sha256=item["reader_sha256"]
        )
        result = owner.query(
            [args.security],
            [args.field],
            as_of=args.as_of,
            mode=args.mode,
            period=args.period,
            basis=args.basis,
        )
        result["archive_binding"] = {
            "path": str(path),
            "sha256": item["sha256"],
            "adapter_path": str(descriptor),
            "transformation": "schema discriminator only; observations/reports/source hashes unchanged",
        }
        emit(result)
        return 0
    if args.command == "financial-archive":
        item = assets["financial-archive"]
        matches = []
        for record in item["runs"]:
            if record["code"] == args.code:
                raw = json.loads(checked(record).read_text())
                matches.append({"path": record["path"], "sha256": record["sha256"], "run": raw})
        if not matches:
            raise ValueError("No registered financial archive for this code")
        emit(
            {
                "dataset": item["id"],
                "historical_pit_certified": False,
                "limits": item["limits"],
                "runs": matches,
            }
        )
        return 0
    if args.command == "factor":
        item = assets["alpha191-" + args.variant]
        values = checked(item)
        statuses = checked(item, "status_path", "status_sha256")

        def cell(path):
            with path.open(newline="") as stream:
                reader = csv.DictReader(stream)
                if args.name not in reader.fieldnames:
                    raise ValueError("Unknown factor column")
                for row in reader:
                    if row["security"] == args.security and row["date"] == args.day:
                        return row[args.name]
            raise ValueError("Requested security/day is absent from this frozen factor result")

        emit(
            {
                "dataset": item["id"],
                "security": args.security,
                "date": args.day,
                "factor": args.name,
                "value": cell(values),
                "status": cell(statuses),
                "source_path": item["path"],
                "sha256": item["sha256"],
                "limits": item["limits"],
            }
        )
        return 0
    raise AssertionError("Unsupported command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as error:
        emit({"error": error.as_dict()})
        raise SystemExit(2)
    except (ValueError, KeyError, OSError) as error:
        emit({"error": str(error)})
        raise SystemExit(2)
