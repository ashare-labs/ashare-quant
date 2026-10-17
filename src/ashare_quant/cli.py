"""JSON CLI; exit 2 for explicit refusals/invalid input, exit 1 for worker failure."""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.backtests.synthetic import specifications
from ashare_quant.contracts import DomainError, RunCreate, StrategyCreate
from ashare_quant.data import capabilities
from ashare_quant.market.reader import ResearchCoverageQuery, ResearchQuery, ResearchReader
from ashare_quant.storage.errors import storage_error
from ashare_quant.storage.store import Store
from ashare_quant.tasks.synthetic.service import Service
from ashare_quant.tasks.synthetic.worker import work_one


def emit(value):
    def encode(obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        raise TypeError(type(obj).__name__)
    print(json.dumps(value, default=encode, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A宽 backend — synthetic execution and exploratory D0 reads")
    parser.add_argument("--db", default=os.getenv("ASHARE_QUANT_DB", ".state/control.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "capabilities", "scenarios", "strategies", "runs", "openapi"):
        sub.add_parser(name)
    sub.add_parser("source-diagnostics-capabilities")
    sub.add_parser("source-diagnostics").add_argument("--request", required=True)
    for name in ("task-catalogue", "tasks", "task-strategies", "task-python-strategies"):
        sub.add_parser(name)
    sub.add_parser("task-data-bind").add_argument("--dataset", required=True)
    sub.add_parser("task-data-import").add_argument("--input", required=True)
    sub.add_parser("task-strategy-register").add_argument("--request", required=True)
    sub.add_parser("task-strategy-get").add_argument("version_id")
    py_register = sub.add_parser("task-strategy-register-python")
    py_register.add_argument("--file", required=True)
    py_register.add_argument("--descriptor", required=True)
    sub.add_parser("task-python-strategy-get").add_argument("version_id")
    sub.add_parser("task-create").add_argument("--request", required=True)
    for name in ("portfolio-catalogue", "portfolio-jobs", "portfolio-strategies"):
        sub.add_parser(name)
    portfolio_import = sub.add_parser("portfolio-data-import")
    portfolio_import.add_argument("--input", required=True)
    portfolio_import.add_argument("--name", required=True)
    for name in ("portfolio-strategy-register", "portfolio-preflight", "portfolio-create"):
        sub.add_parser(name).add_argument("--request", required=True)
    for name in ("portfolio-status", "portfolio-manifest", "portfolio-logs", "portfolio-cancel", "portfolio-resume", "portfolio-result"):
        sub.add_parser(name).add_argument("job_id")
    for name in ("task-status", "task-manifest", "task-logs", "task-cancel", "task-resume", "task-result"):
        sub.add_parser(name).add_argument("job_id")
    sub.add_parser("task-service")
    sub.add_parser("database-upgrade").add_argument("--destination", required=True)
    task_worker = sub.add_parser("task-worker")
    task_worker.add_argument("--task-id", dest="job_id")
    task_worker.add_argument("--once", action="store_true")
    strategy = sub.add_parser("strategy-create")
    strategy.add_argument("--name", required=True)
    strategy.add_argument("--scenario", required=True, choices=list(specifications()))
    strategy.add_argument("--engine", choices=["fixture-replay-v1", "rqalpha"], default="fixture-replay-v1")
    run = sub.add_parser("run-create")
    run.add_argument("--strategy", required=True)
    run.add_argument("--key", required=True)
    run.add_argument("--account", default="paper-demo")
    run.add_argument("--mode", choices=["fixture", "synthetic", "research", "paper"], default="fixture")
    run.add_argument("--engine", choices=["fixture-replay-v1", "rqalpha"], default="fixture-replay-v1")
    for name in ("status", "state", "manifest", "logs", "cancel", "receipt"):
        sub.add_parser(name).add_argument("run_id")
    worker = sub.add_parser("worker")
    worker.add_argument("--run-id")
    worker.add_argument("--once", action="store_true")
    demo = sub.add_parser("demo")
    demo.add_argument("--scenario", default="buy_hold", choices=list(specifications()))
    demo.add_argument("--key", default="demo-buy-hold-v1")
    demo.add_argument("--engine", choices=["fixture-replay-v1", "rqalpha"], default="fixture-replay-v1")
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    for command in ("market-describe", "market-read", "market-coverage"):
        research = sub.add_parser(command)
        research.add_argument("--store", required=True)
        research.add_argument("--dataset", required=True)
        if command != "market-describe":
            research.add_argument("--request", required=True, help="Path to a typed JSON query; no collection")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        if args.command == "database-upgrade":
            from ashare_quant.storage.upgrade import upgrade_copy
            emit(upgrade_copy(args.db, args.destination))
            return 0
        if args.command in ("source-diagnostics-capabilities", "source-diagnostics"):
            from ashare_quant.diagnostics.contracts import DiagnosticQuery
            from ashare_quant.diagnostics.service import Diagnostics
            diagnostics = Diagnostics()
            if args.command == "source-diagnostics-capabilities":
                emit(diagnostics.capabilities())
            else:
                query = DiagnosticQuery.model_validate_json(Path(args.request).read_text())
                emit(diagnostics.diagnose(query))
            return 0  # A diagnostic report, including missing capabilities, never grants execution.
        if args.command.startswith("portfolio-"):
            from ashare_quant.tasks.portfolio.contracts import (
                PortfolioCreate,
                PortfolioSelection,
                PortfolioStrategyDefinition,
            )
            from ashare_quant.tasks.portfolio.service import PortfolioJobs
            jobs = PortfolioJobs(Store(args.db))
            if args.command == "portfolio-data-import":
                emit(jobs.import_data(Path(args.input), args.name))
            elif args.command in ("portfolio-catalogue", "portfolio-jobs", "portfolio-strategies"):
                emit(jobs.catalogue() if args.command == "portfolio-catalogue" else
                     jobs.list() if args.command == "portfolio-jobs" else jobs.strategies.list())
            elif args.command in ("portfolio-create", "portfolio-preflight", "portfolio-strategy-register"):
                kind, method = {"portfolio-create": (PortfolioCreate, jobs.create),
                    "portfolio-preflight": (PortfolioSelection, jobs.preflight),
                    "portfolio-strategy-register": (PortfolioStrategyDefinition, jobs.register_strategy)}[args.command]
                emit(method(kind.model_validate_json(Path(args.request).read_text())))
            elif args.command == "portfolio-manifest":
                emit(jobs.check(jobs.repo.row(args.job_id)))
            else:
                operation = {"portfolio-status": "get", "portfolio-logs": "logs", "portfolio-cancel": "cancel",
                             "portfolio-resume": "resume", "portfolio-result": "result"}[args.command]
                emit(getattr(jobs, operation)(args.job_id))
            return 0
        if args.command == "tasks" or args.command.startswith("task-"):
            from ashare_quant.tasks.daily.contracts import JobCreate
            from ashare_quant.tasks.daily.service import Jobs
            from ashare_quant.tasks.worker import work_one as work_job
            jobs = Jobs(Store(args.db))
            if args.command == "tasks":
                emit(jobs.list())
            elif args.command == "task-service":
                emit(jobs.service_status())
            elif args.command == "task-strategy-register-python":
                from ashare_quant.strategies.python.spec import PythonDescriptor
                emit(jobs.register_python(args.file, PythonDescriptor.model_validate_json(Path(args.descriptor).read_text())))
            elif args.command == "task-python-strategies":
                emit(jobs.python_strategies())
            elif args.command == "task-python-strategy-get":
                emit(jobs.python_strategy(args.version_id))
            elif args.command == "task-strategy-register":
                from ashare_quant.strategies.daily.rules import RuleStrategyDefinition
                emit(jobs.register_strategy(RuleStrategyDefinition.model_validate_json(Path(args.request).read_text())))
            elif args.command == "task-strategies":
                emit(jobs.list_strategies())
            elif args.command == "task-strategy-get":
                emit(jobs.get_strategy(args.version_id))
            elif args.command == "task-catalogue":
                emit(jobs.catalogue())
            elif args.command == "task-data-bind":
                emit(jobs.bind(args.dataset))
            elif args.command == "task-data-import":
                emit(jobs.import_data(Path(args.input)))
            elif args.command == "task-create":
                emit(jobs.create(JobCreate.model_validate_json(Path(args.request).read_text())))
            elif args.command == "task-manifest":
                emit(jobs.check(jobs.repo.row(args.job_id)))
            elif args.command == "task-worker":
                if args.job_id or args.once:
                    processed = work_job(jobs, args.job_id)
                    if processed:
                        jobs = jobs.for_job(jobs.repo.row(processed))
                    emit({"processed_job_id": processed, "job": jobs.get(processed) if processed else None})
                    return 1 if processed and jobs.get(processed).status == "failed" else 0
                try:
                    while True:
                        if work_job(jobs) is None:
                            time.sleep(1)
                except KeyboardInterrupt:
                    return 0
            else:
                operation = {"task-status": "get", "task-logs": "logs", "task-cancel": "cancel",
                             "task-resume": "resume", "task-result": "result"}[args.command]
                emit(getattr(jobs, operation)(args.job_id))
            return 0
        if args.command == "capabilities":
            emit(capabilities())
            return 0
        if args.command == "scenarios":
            emit(specifications())
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
        if args.command == "serve":
            import uvicorn
            uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)
            return 0
        if args.command == "openapi":
            emit(create_app(args.db).openapi())
            return 0
        store = Store(args.db)
        service = Service(store)
        match args.command:
            case "init":
                emit({"database": str(store.path), "schema_version": 4})
            case "strategy-create":
                emit(service.register_strategy(StrategyCreate(name=args.name, scenario=args.scenario, engine_id=args.engine)))
            case "strategies":
                emit(store.strategies())
            case "runs":
                emit(store.runs())
            case "run-create":
                emit(service.submit(RunCreate(strategy_version_id=args.strategy, account_id=args.account,
                                             engine_id=args.engine, mode=args.mode,
                                             idempotency_key=args.key)))
            case "status":
                emit(store.run(args.run_id))
            case "state" | "manifest" | "logs" | "cancel" | "receipt":
                emit(getattr(store, args.command)(args.run_id))
            case "worker":
                if args.run_id or args.once:
                    emit({"processed_run_id": work_one(store, args.run_id)})
                else:
                    try:
                        while True:
                            if work_one(store) is None:
                                time.sleep(1)
                    except KeyboardInterrupt:
                        return 0
            case "demo":
                version = service.register_strategy(StrategyCreate(name="Manual demo", scenario=args.scenario,
                                                                  engine_id=args.engine))
                run = service.submit(RunCreate(strategy_version_id=version.version_id,
                                              account_id="fixture-demo", idempotency_key=args.key,
                                              engine_id=args.engine, mode="synthetic" if args.engine == "rqalpha" else "fixture"))
                if run.status == "queued":
                    work_one(store, run.run_id)
                emit({"notice": "RQALPHA NATIVE ENGINE / SYNTHETIC DATA / NO REAL MARKET VALIDATION"
                                if args.engine == "rqalpha" else "SYNTHETIC TEST DOUBLE; NO REAL STRATEGY EXECUTION",
                      "state": store.state(run.run_id), "manifest": store.manifest(run.run_id)})
            case "backup":
                store.backup(args.destination)
                emit({"backup": args.destination})
        return 0
    except DomainError as exc:
        emit({"error": exc.body()})
        return 2
    except (OSError, sqlite3.Error) as exc:
        emit({"error": storage_error(exc).body()})
        return 2
    except ValidationError as exc:
        emit({"error": {"code": "INVALID_INPUT", "message": str(exc)}})
        return 2
    except Exception as exc:
        emit({"error": {"code": "UNEXPECTED_ERROR", "type": type(exc).__name__}})
        return 1


if __name__ == "__main__":
    sys.exit(main())
