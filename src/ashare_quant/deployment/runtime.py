"""Local foreground commands, explicit initialization and read-only diagnostics."""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from ashare_quant.contracts import DomainError


@contextmanager
def deployment_lock(config, *, exclusive=False):
    """Backup excludes cooperating foreground servers for their entire lifetime."""
    root = Path(tempfile.gettempdir()) / f"ashare-deployment-locks-{os.getuid()}"
    root.mkdir(exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DomainError("DEPLOYMENT_LOCK_UNSAFE", "Deployment lock directory is unsafe")
    key = hashlib.sha256(str(config.control_db).encode()).hexdigest()
    fd = os.open(root / (key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DomainError(
                "DEPLOYMENT_BUSY",
                "Stop this deployment's foreground API/worker/paper before backup",
                409,
            ) from exc
        yield
    finally:
        os.close(fd)


def commands(config):
    prefix = [
        sys.executable,
        "-m",
        "ashare_quant.deployment",
        "--deployment",
        str(config.deployment),
        "--profile",
        config.profile,
    ]
    return {
        "profile": config.profile,
        "control_db": str(config.control_db),
        "paper_root": str(config.paper_root),
        "api": prefix + ["api"],
        "task_worker": prefix + ["task-worker"],
        "paper": prefix + ["paper"],
        "web": {
            "argv": ["npm", "run", "dev"],
            "cwd": "quant-web checkout",
            "environment": {
                "FRONTEND_PORT": str(config.web_port),
                "ASHARE_API_URL": f"http://127.0.0.1:{config.api_port}",
                "ASHARE_PAPER_URL": f"http://127.0.0.1:{config.paper_port}",
            },
        },
    }


def _database_check(path, required):
    """Schema reads cannot create a DB, journal or SHM in the source directory.

    Immutable mode reads the main file only. If uncheckpointed DDL exists in WAL,
    readiness is refused until the owner closes/checkpoints it; no false readiness.
    """
    result = {"database": str(path), "readable": False, "missing_tables": []}
    if not path.is_file():
        return result | {"error": "DATABASE_MISSING"}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        missing = sorted(set(required) - tables)
        return result | {
            "readable": not missing,
            "missing_tables": missing,
            "wal_present": Path(str(path) + "-wal").exists(),
            "inspection": "read_only_main_file_schema",
        }
    except sqlite3.Error as exc:
        return result | {"error": "DATABASE_UNREADABLE", "message": str(exc)}


def doctor(config):
    """Inspect files/runtime only; never instantiate a Store or a service."""
    checks = [
        {
            "name": "control_db",
            "path": str(config.control_db),
            "present": config.control_db.is_file(),
        },
        {
            "name": "paper_root",
            "path": str(config.paper_root),
            "present": config.paper_root.is_dir(),
        },
    ]
    checks.extend(
        {
            "name": item.name,
            "kind": item.kind,
            "role": item.role,
            "retention": item.retention,
            "path": str(item.path),
            "present": item.path.exists(),
            "selected": item.name in config.selected_assets,
        }
        for item in config.assets
    )
    databases = {
        "control_db": _database_check(config.control_db, {"tasks", "runs", "task_service"}),
        "paper_root": _database_check(
            config.paper_root / "control.sqlite3", {"paper_sessions", "paper_days"}
        ),
    }
    for asset in config.assets:
        if asset.name in config.selected_assets and asset.kind in {"store", "local_daily"}:
            databases[asset.name] = _database_check(
                asset.path / ("daily.sqlite3" if asset.kind == "local_daily" else "catalog.sqlite"),
                {"bars", "instruments", "calendar", "responses"}
                if asset.kind == "local_daily"
                else {"meta", "batches", "reports", "snapshots"},
            )
    runtime = {}
    from ashare_quant.providers.runtime import verify
    from ashare_quant.runtime import runtime_fingerprint

    for name, function in (
        ("data_owner", verify),
        ("native", lambda: runtime_fingerprint(native=True)),
    ):
        try:
            function()
            runtime[name] = {"ready": True}
        except (DomainError, ImportError) as exc:
            runtime[name] = {
                "ready": False,
                "error": getattr(exc, "code", "RUNTIME_MISSING"),
                "message": str(exc),
            }
    market = None
    if config.document["profiles"][config.profile].get("market_release"):
        try:
            from ashare_data.core.model import DataError
            from ashare_data.product.configuration import resolve as owner_configuration
            from ashare_data.product.market_release import open_release

            release = open_release(owner_configuration(config.deployment, profile=config.profile))
            market = {
                "ready": True,
                **release.receipt(),
                "verification": "manifest chain and frozen metadata; full object check uses release_cli verify",
            }
        except (DataError, OSError, ValueError, KeyError) as exc:
            market = {"ready": False, "error": getattr(exc, "code", type(exc).__name__)}
    missing = [
        item["name"] for item in checks if not item["present"] and item.get("selected", True)
    ]
    invalid = [name for name, value in databases.items() if not value["readable"]]
    from ashare_quant.deployment.backup import reference_asset_checks

    references = reference_asset_checks(config)
    invalid_references = [name for name, value in references.items() if not value["ready"]]
    return {
        "schema": "ashare-deployment-doctor.v1",
        "profile": config.profile,
        "kind": config.kind,
        "data_root": str(config.data_root),
        "configured": True,
        "ready": not missing
        and not invalid
        and not invalid_references
        and all(v["ready"] for v in runtime.values())
        and (market is None or market["ready"]),
        "market_release": market,
        "missing": missing,
        "invalid_databases": invalid,
        "databases": databases,
        "invalid_reference_assets": invalid_references,
        "reference_assets": references,
        "runtime": runtime,
        "checks": checks,
        "commands": commands(config),
        "modified_source": False,
    }


def initialize(config):
    """Only this explicit command initializes absent runtime databases."""
    from ashare_quant.paper.service import PaperService
    from ashare_quant.storage.store import Store
    from ashare_quant.tasks.daily.executor import Configuration
    from ashare_quant.tasks.daily.service import Jobs

    source = _database_check(
        config.daily_root / "daily.sqlite3", {"bars", "instruments", "calendar", "responses"}
    )
    if not source["readable"]:
        raise DomainError(
            "DEPLOYMENT_SOURCE_MISSING",
            "Main daily.sqlite3 source is missing or has an invalid schema",
            503,
        )
    with deployment_lock(config, exclusive=True):
        Store(config.control_db)
        Jobs(Store(config.control_db), Configuration(config.owner_store_root))
        PaperService(config.paper_root)
        if not config.release_store_root.exists():
            from ashare_data import Store as DataStore

            DataStore.init(config.release_store_root)
    return doctor(config)


def require_initialized(config):
    if not config.control_db.is_file() or not (config.paper_root / "control.sqlite3").is_file():
        raise DomainError("DEPLOYMENT_NOT_INITIALIZED", "Run the explicit init command first", 503)


def run(config, component, *, once=False, task_id=None):
    import time

    require_initialized(config)
    with deployment_lock(config):
        if component == "api":
            import uvicorn

            from ashare_quant.api import create_app
            from ashare_quant.tasks.daily.executor import Configuration

            app = create_app(
                str(config.control_db),
                daily_tasks_configuration=Configuration(config.owner_store_root),
                product_configuration=config,
            )
            uvicorn.run(app, host="127.0.0.1", port=config.api_port)
        elif component == "paper":
            import uvicorn

            from ashare_quant.paper.api import create_app

            uvicorn.run(create_app(config.paper_root), host="127.0.0.1", port=config.paper_port)
        elif component == "task-worker":
            from ashare_quant.storage.store import Store
            from ashare_quant.tasks.daily.executor import Configuration
            from ashare_quant.tasks.daily.service import Jobs
            from ashare_quant.tasks.worker import work_one

            jobs = Jobs(Store(config.control_db), Configuration(config.owner_store_root))
            while True:
                processed = work_one(jobs, task_id)
                if once or task_id:
                    status = (
                        jobs.for_job(jobs.repo.row(processed)).get(processed) if processed else None
                    )
                    print(
                        json.dumps(
                            {
                                "processed_job_id": processed,
                                "control_db": str(config.control_db),
                                "job": status.model_dump(mode="json") if status else None,
                            }
                        )
                    )
                    return int(status is not None and status.status == "failed")
                if processed is None:
                    time.sleep(1)
    return 0
