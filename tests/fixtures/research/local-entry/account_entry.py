"""Local, read-only registration adapter for reviewed OnlineService ledgers.

This adapter is deployment configuration, outside AQ's pinned execution package.
It neither imports accounts into SQL paper sessions nor exposes execution actions.
"""
import hashlib
import json
import re
import stat
from decimal import Decimal
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.service import engine_pin
from ashare_quant.storage.artifacts import digest

CONTRACT = "local-online-accounts.v1"
REGISTRY = "ashare-online-account-registry.v1"


def require(value, code, message, status=409):
    if not value:
        raise DomainError(code, message, status)


def regular(path):
    require(stat.S_ISREG(path.lstat().st_mode), "ACCOUNT_FILE_UNSAFE", "账户文件必须是普通文件。")
    return path.read_bytes()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def registered(root):
    directory = Path(root) / "state/online"
    require(directory.is_dir() and not directory.is_symlink(), "ACCOUNT_REGISTRY_MISSING", "尚未登记已结算账户。", 503)
    value = json.loads(regular(directory / "registry.json"))
    require(value.get("schema") == REGISTRY and isinstance(value.get("accounts"), list), "ACCOUNT_REGISTRY_INVALID", "账户登记格式不匹配。")
    entries = value["accounts"]
    ids = [entry["account_id"] for entry in entries]
    require(len(ids) == len(set(ids)), "ACCOUNT_REGISTRY_CONFLICT", "账户登记存在重复身份。")
    require(all(isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", item) for item in ids), "ACCOUNT_REGISTRY_INVALID", "账户编号格式无效。")
    return directory, entries


def view(root, account_id):
    directory, entries = registered(root)
    matches = [entry for entry in entries if entry["account_id"] == account_id]
    require(len(matches) == 1, "ACCOUNT_NOT_FOUND", "未登记此账户。", 404)
    entry = matches[0]
    require(entry.get("path") == account_id, "ACCOUNT_PATH_INVALID", "账户目录必须对应登记编号。")
    account = directory / entry["path"]
    require(account.is_dir() and not account.is_symlink(), "ACCOUNT_PATH_INVALID", "账户目录不可用。")
    inventory = entry["files"]
    actual = sorted(p.relative_to(account).as_posix() for p in account.rglob("*") if p.is_file() and not p.name.endswith(".lock"))
    require(actual == sorted(inventory), "ACCOUNT_FILES_CHANGED", "账户文件范围已变化，需重新核验登记。")
    for name, expected in inventory.items():
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "ACCOUNT_PATH_INVALID", "账户文件路径无效。")
        current = account
        for part in relative.parts:
            current /= part
            require(not current.is_symlink(), "ACCOUNT_FILE_UNSAFE", "账户文件不可通过链接替换。")
        raw = regular(current)
        require(len(raw) == expected["bytes"] and sha(raw) == expected["sha256"], "ACCOUNT_CONTENT_CHANGED", "账户文件与登记摘要不一致。")
    require(digest(engine_pin()) == entry["runtime_pin_sha256"], "ACCOUNT_RUNTIME_CHANGED", "正式运行时已变化，需显式核验迁移。")
    service = OnlineService(account)
    state, config = service._load()
    require(config.account_id == account_id and state["identity"] == entry["identity"] and state["head"] == entry["head"], "ACCOUNT_IDENTITY_CHANGED", "账户身份或检查点与登记不一致。")
    require(all(day["status"] == "committed" for day in state["days"].values()), "ACCOUNT_NOT_SETTLED", "仅展示已核验结算的账户。")
    authorization = state["settled_runtime_authorization"]
    require(authorization["review_sha256"] == entry["review_sha256"], "ACCOUNT_REVIEW_CHANGED", "迁移复核摘要与登记不一致。")
    report = service.report(state["last_day"], "closing")
    native = report["native"]
    require(sha(regular(service.path)) == inventory["state.json"]["sha256"], "ACCOUNT_CONTENT_CHANGED", "读取期间账户发生变化，请重新读取。")
    return {
        "contract_version": CONTRACT,
        "account_id": account_id,
        "label": entry["label"],
        "identity": state["identity"],
        "head": state["head"],
        "last_day": state["last_day"],
        "days": sorted(state["days"]),
        "model": config.model,
        "status": "settled",
        "runtime_compatible": True,
        "read_only": True,
        "next_day_authorized": False,
        "account": native["account"],
        "orders": list(native["orders"].values()),
        "trades": native["trades"],
        "fees": str(sum(Decimal(trade["cost"]) for trade in native["trades"])),
        "cursor": native["cursor"],
        "settlement_count": native["settlement_count"],
        "genesis": native["genesis"],
        "review_sha256": entry["review_sha256"],
        "limitations": ["声明初始库存的独立模拟账户。", "费用沿用原模型，未单独计过户费。", "下一交易日尚未授权，须先核验当日来源与行动窗口。"],
    }


def catalogue(root):
    _, entries = registered(root)
    keys = ("account_id", "label", "identity", "last_day", "status", "runtime_compatible")
    return {"contract_version": CONTRACT, "accounts": [{key: value[key] for key in keys} for value in (view(root, entry["account_id"]) for entry in entries)]}


def mount(app, root):
    from fastapi.responses import JSONResponse

    def respond(action):
        try:
            return JSONResponse(action(), headers={"Cache-Control": "no-store"})
        except DomainError as exc:
            return JSONResponse({"error": exc.body()}, status_code=exc.status, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({"error": {"code": "ACCOUNT_REGISTRY_UNAVAILABLE", "message": "账户登记或证据不可读取，请核验本机配置。"}}, status_code=503, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/online-accounts", tags=["registered online accounts"])
    def list_accounts():
        return respond(lambda: catalogue(root))

    @app.get("/api/v1/online-accounts/{account_id}", tags=["registered online accounts"])
    def read_account(account_id: str):
        return respond(lambda: view(root, account_id))


def run_api(root):
    import uvicorn

    from ashare_quant.api import create_app
    from ashare_quant.deployment import resolve
    from ashare_quant.deployment.runtime import deployment_lock, require_initialized
    from ashare_quant.tasks.executor import Configuration

    config = resolve(Path(root) / "deployment.json", "product")
    require_initialized(config)
    with deployment_lock(config):
        app = create_app(str(config.control_db), daily_tasks_configuration=Configuration(config.owner_store_root), product_configuration=config)
        mount(app, root)
        uvicorn.run(app, host="127.0.0.1", port=config.api_port)
    return 0
