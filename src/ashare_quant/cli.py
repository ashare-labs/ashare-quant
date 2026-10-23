"""JSON operations for custom Python research, shared tasks and historical reads."""
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.data import capabilities
from ashare_quant.market.reader import ResearchCoverageQuery, ResearchQuery, ResearchReader
from ashare_quant.storage.errors import storage_error
from ashare_quant.storage.store import Store
from ashare_quant.tasks.executor import Configuration
from ashare_quant.tasks.history import History
from ashare_quant.tasks.service import Jobs
from ashare_quant.tasks.worker import work_one


def emit(value):
    print(json.dumps(value, default=lambda obj: obj.model_dump(mode="json"), ensure_ascii=False, indent=2))

def selected_runtime(args):
    """Resolve state before opening it; legacy entry points share the deployment."""
    stateless = {"capabilities", "source-diagnostics-capabilities", "source-diagnostics"}
    if args.command in stateless or args.command.startswith("market-"):
        return None
    from ashare_quant.deployment.configuration import deployment_path

    selected = deployment_path(args.deployment)
    if selected:
        from ashare_quant.deployment import resolve
        from ashare_quant.deployment.runtime import require_initialized

        config = resolve(args.deployment, args.profile)
        # As in create_app, an environment DB never overrides the manifest. An
        # explicit conflicting flag is an error, not permission to fork state.
        if args.db is not None and Path(args.db).expanduser().resolve() != config.control_db:
            raise DomainError("PRODUCT_STATE_MISMATCH", "--db differs from the selected deployment", 409)
        if args.command == "serve" and args.port is not None and args.port != config.api_port:
            raise DomainError("DEPLOYMENT_PORT_MISMATCH", "--port differs from the selected API port", 409)
        args.db = str(config.control_db)
        if args.command != "init":
            require_initialized(config)
        return config
    args.db = args.db or os.getenv("ASHARE_QUANT_DB")
    if not args.db:
        raise DomainError(
            "CLI_STATE_NOT_CONFIGURED",
            "Select --deployment / ASHARE_DEPLOYMENT, or an explicit --db / ASHARE_QUANT_DB; "
            "no working-directory state is selected automatically",
            503,
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--deployment")
    parser.add_argument("--profile", default="product")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "capabilities", "openapi", "task-service", "history", "history-strategies"):
        sub.add_parser(name)
    worker = sub.add_parser("task-worker")
    worker.add_argument("--task-id", dest="job_id")
    worker.add_argument("--once", action="store_true")
    for name in ("task-status", "task-manifest", "task-logs", "task-result", "task-cancel", "task-resume"):
        sub.add_parser(name).add_argument("job_id")
    sub.add_parser("python-create").add_argument("--request", required=True)
    sub.add_parser("backup").add_argument("destination")
    sub.add_parser("database-upgrade").add_argument("--destination", required=True)
    sub.add_parser("source-diagnostics-capabilities")
    sub.add_parser("source-diagnostics").add_argument("--request", required=True)
    for name in ("market-describe", "market-read", "market-coverage"):
        command = sub.add_parser(name)
        command.add_argument("--store", required=True)
        command.add_argument("--dataset", required=True)
        if name != "market-describe":
            command.add_argument("--request", required=True)
    sub.add_parser("serve").add_argument("--port", type=int)
    args = parser.parse_args(argv)
    try:
        configuration = selected_runtime(args)
        if configuration is not None and args.command in {"init", "serve", "task-worker", "backup"}:
            from ashare_quant.deployment.backup import create_backup
            from ashare_quant.deployment.runtime import initialize, run
            if args.command == "init":
                emit(initialize(configuration))
            elif args.command == "backup":
                emit(create_backup(configuration, args.destination))
            else:
                return run(configuration, "api" if args.command == "serve" else "task-worker",
                           once=getattr(args, "once", False), task_id=getattr(args, "job_id", None))
            return 0
        if args.command == "capabilities":
            emit(capabilities())
            return 0
        if args.command.startswith("source-diagnostics"):
            from ashare_quant.diagnostics.contracts import DiagnosticQuery
            from ashare_quant.diagnostics.service import Diagnostics
            value = Diagnostics()
            emit(value.capabilities() if args.command.endswith("capabilities") else
                 value.diagnose(DiagnosticQuery.model_validate_json(Path(args.request).read_text())))
            return 0
        if args.command.startswith("market-"):
            reader = ResearchReader(args.store)
            if args.command == "market-describe":
                emit(reader.describe(args.dataset))
            elif args.command == "market-read":
                emit(reader.read(args.dataset, ResearchQuery.model_validate_json(Path(args.request).read_text())))
            else:
                emit(reader.coverage(args.dataset, ResearchCoverageQuery.model_validate_json(Path(args.request).read_text())))
            return 0
        if args.command == "database-upgrade":
            from ashare_quant.storage.upgrade import upgrade_copy
            emit(upgrade_copy(args.db, args.destination))
            return 0
        if args.command in {"serve", "openapi", "python-create"}:
            app = create_app(args.db, product_configuration=configuration)
            if args.command != "serve" and getattr(app.state, "database_error", None):
                emit({"error": app.state.database_error})
                return 2
            if args.command == "serve":
                import uvicorn
                uvicorn.run(app, host="127.0.0.1", port=args.port or 18839)
            elif args.command == "openapi":
                emit(app.openapi())
            else:
                from ashare_quant.tasks.minute.contracts import PythonCreate
                emit(app.state.python_jobs.create(PythonCreate.model_validate_json(Path(args.request).read_text())))
            return 0
        store = Store(args.db)
        jobs = Jobs(store, Configuration(configuration.owner_store_root) if configuration else None)
        history = History(store, jobs.config)
        if args.command == "init":
            emit({"database": str(store.path), "schema_version": 4})
        elif args.command == "backup":
            store.backup(args.destination)
            emit({"backup": args.destination, "scope": "sqlite_only", "complete_recovery": False})
        elif args.command == "history":
            emit(history.list())
        elif args.command == "history-strategies":
            emit(history.strategies())
        elif args.command == "task-service":
            emit(jobs.service_status())
        elif args.command == "task-worker":
            while True:
                processed = work_one(jobs, args.job_id)
                if args.once or args.job_id:
                    view = jobs.for_job(jobs.repo.row(processed)).get(processed) if processed else None
                    emit({"processed_job_id": processed, "job": view})
                    return int(view is not None and view.status == "failed")
                if processed is None:
                    time.sleep(1)
        elif args.command == "task-manifest":
            emit(history.check(history.repo.row(args.job_id)))
        elif args.command in {"task-cancel", "task-resume"}:
            target = jobs.for_job(jobs.repo.row(args.job_id))
            emit(getattr(target, args.command.removeprefix("task-"))(args.job_id))
        else:
            method = {"task-status": "get", "task-logs": "logs", "task-result": "result"}[args.command]
            emit(getattr(history, method)(args.job_id))
        return 0
    except KeyboardInterrupt:
        return 0
    except DomainError as exc:
        emit({"error": exc.body()})
        return 2
    except (OSError, sqlite3.Error) as exc:
        emit({"error": storage_error(exc).body()})
        return 2
    except ValidationError as exc:
        emit({"error": {"code": "INVALID_REQUEST", "message": str(exc)}})
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
