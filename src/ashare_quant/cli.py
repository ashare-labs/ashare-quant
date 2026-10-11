"""JSON CLI; exit 2 for explicit refusals/invalid input, exit 1 for worker failure."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from .api import create_app
from .contracts import DomainError, RunCreate, StrategyCreate
from .d1 import (
    D1CheckRequest,
    PreviewRequest,
    describe_plan,
    preflight,
    preview,
    refuse_execution,
)
from .d1_br1 import BR1Request, br1_preflight, describe_br1_profile, refuse_br1_execution
from .d1_codec import BR1BindRequest, FrozenBR1Reader
from .data import capabilities
from .engine import specifications
from .research import ResearchCoverageQuery, ResearchQuery, ResearchReader
from .service import Service
from .store import Store
from .worker import work_one


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
    sub.add_parser("d1-plan")
    sub.add_parser("d1-br1-profile")
    sub.add_parser("backtest-capabilities")
    sub.add_parser("backtest-strategies")
    sub.add_parser("source-diagnostics-capabilities")
    sub.add_parser("source-diagnostics").add_argument("--request", required=True)
    for name in ("job-catalogue", "jobs", "job-strategies", "job-python-strategies"):
        sub.add_parser(name)
    sub.add_parser("job-data-bind").add_argument("--dataset", required=True)
    sub.add_parser("job-data-import").add_argument("--input", required=True)
    sub.add_parser("job-strategy-register").add_argument("--request", required=True)
    sub.add_parser("job-strategy-get").add_argument("version_id")
    py_register = sub.add_parser("job-strategy-register-python")
    py_register.add_argument("--file", required=True)
    py_register.add_argument("--descriptor", required=True)
    sub.add_parser("job-python-strategy-get").add_argument("version_id")
    sub.add_parser("job-create").add_argument("--request", required=True)
    for name in ("job-status", "job-manifest", "job-logs", "job-cancel", "job-resume", "job-result"):
        sub.add_parser(name).add_argument("job_id")
    task_worker = sub.add_parser("job-worker")
    task_worker.add_argument("--job-id")
    task_worker.add_argument("--once", action="store_true")
    sub.add_parser("backtest-strategy-register").add_argument("--definition", required=True)
    for command in ("backtest-preflight", "backtest-submit"):
        sub.add_parser(command).add_argument("--request", required=True)
    binding = sub.add_parser("d1-br1-bind")
    binding.add_argument("--store", required=True)
    binding.add_argument("--dataset", required=True)
    binding.add_argument("--request", required=True, help="Explicit conditional mode and backend profile hash")
    for name in ("d1-br1-preflight", "d1-br1-run"):
        sub.add_parser(name).add_argument("--request", required=True, help="Conditional BR1 contract; execution blocked")
    for name in ("d1-preflight", "d1-intent-preview", "d1-run"):
        sub.add_parser(name).add_argument("--request", required=True, help="Typed JSON; D1 execution stays blocked")
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
    for command in ("research-describe", "research-read", "research-coverage"):
        research = sub.add_parser(command)
        research.add_argument("--store", required=True)
        research.add_argument("--dataset", required=True)
        if command != "research-describe":
            research.add_argument("--request", required=True, help="Path to a typed JSON query; no collection")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        if args.command in ("source-diagnostics-capabilities", "source-diagnostics"):
            from .diagnostics.contracts import DiagnosticQuery
            from .diagnostics.service import Diagnostics
            diagnostics = Diagnostics()
            if args.command == "source-diagnostics-capabilities":
                emit(diagnostics.capabilities())
            else:
                query = DiagnosticQuery.model_validate_json(Path(args.request).read_text())
                emit(diagnostics.diagnose(query))
            return 0  # A diagnostic report, including missing capabilities, never grants execution.
        if args.command == "jobs" or args.command.startswith("job-"):
            from .m2_jobs.contracts import JobCreate
            from .m2_jobs.service import Jobs
            from .m2_jobs.worker import work_one as work_job
            jobs = Jobs(Store(args.db))
            if args.command == "jobs":
                emit(jobs.list())
            elif args.command == "job-strategy-register-python":
                from .python_strategies.spec import PythonDescriptor
                emit(jobs.register_python(args.file, PythonDescriptor.model_validate_json(Path(args.descriptor).read_text())))
            elif args.command == "job-python-strategies":
                emit(jobs.python_strategies())
            elif args.command == "job-python-strategy-get":
                emit(jobs.python_strategy(args.version_id))
            elif args.command == "job-strategy-register":
                from .m2.rules import RuleStrategyDefinition
                emit(jobs.register_strategy(RuleStrategyDefinition.model_validate_json(Path(args.request).read_text())))
            elif args.command == "job-strategies":
                emit(jobs.list_strategies())
            elif args.command == "job-strategy-get":
                emit(jobs.get_strategy(args.version_id))
            elif args.command == "job-catalogue":
                emit(jobs.catalogue())
            elif args.command == "job-data-bind":
                emit(jobs.bind(args.dataset))
            elif args.command == "job-data-import":
                emit(jobs.import_data(Path(args.input)))
            elif args.command == "job-create":
                emit(jobs.create(JobCreate.model_validate_json(Path(args.request).read_text())))
            elif args.command == "job-manifest":
                emit(jobs.check(jobs.repo.row(args.job_id)))
            elif args.command == "job-worker":
                if args.job_id or args.once:
                    processed = work_job(jobs, args.job_id)
                    emit({"processed_job_id": processed, "job": jobs.get(processed) if processed else None})
                    return 1 if processed and jobs.get(processed).status == "failed" else 0
                try:
                    while True:
                        if work_job(jobs) is None:
                            time.sleep(1)
                except KeyboardInterrupt:
                    return 0
            else:
                operation = {"job-status": "get", "job-logs": "logs", "job-cancel": "cancel",
                             "job-resume": "resume", "job-result": "result"}[args.command]
                emit(getattr(jobs, operation)(args.job_id))
            return 0
        if args.command.startswith("backtest-"):
            from .m2.api import Capabilities
            from .m2.catalogue import production_catalogue
            from .m2.contracts import BacktestRequest, StrategyDefinition
            from .m2.preflight import preflight as m2_preflight
            from .m2.preflight import refuse_submission
            from .m2.registry import StrategyRegistry
            if args.command == "backtest-capabilities":
                emit(Capabilities())
                return 0
            if args.command == "backtest-submit":
                BacktestRequest.model_validate_json(Path(args.request).read_text())
                refuse_submission()
            registry = StrategyRegistry(Store(args.db))
            if args.command == "backtest-strategy-register":
                emit(registry.register(StrategyDefinition.model_validate_json(Path(args.definition).read_text())))
                return 0
            if args.command == "backtest-strategies":
                emit(registry.list())
                return 0
            emit(m2_preflight(BacktestRequest.model_validate_json(Path(args.request).read_text()), registry, production_catalogue()))
            return 2  # Preparation report never grants execution.
        if args.command == "d1-br1-bind":
            request = BR1BindRequest.model_validate_json(Path(args.request).read_text())
            emit(FrozenBR1Reader(args.store, dataset_id=args.dataset, mode=request.mode,
                                 backend_profile_sha256=request.backend_profile_sha256).snapshot().report)
            return 0  # Successful read only; report.execution_permission is always false.
        if args.command == "d1-br1-profile":
            emit(describe_br1_profile())
            return 0
        if args.command.startswith("d1-br1-"):
            result = br1_preflight(BR1Request.model_validate_json(Path(args.request).read_text()))
            if args.command == "d1-br1-run":
                refuse_br1_execution()
            emit(result)
            return 2
        if args.command == "d1-plan":
            emit(describe_plan())
            return 0
        if args.command.startswith("d1-"):
            raw = Path(args.request).read_text()
            if args.command == "d1-intent-preview":
                emit(preview(PreviewRequest.model_validate_json(raw)))
                return 0
            result = preflight(D1CheckRequest.model_validate_json(raw))
            if args.command == "d1-run":
                refuse_execution()
            emit(result)
            return 2  # A diagnostic response is never execution admission.
        if args.command == "capabilities":
            emit(capabilities())
            return 0
        if args.command == "scenarios":
            emit(specifications())
            return 0
        if args.command.startswith("research-"):
            reader = ResearchReader(args.store)
            if args.command == "research-describe":
                emit(reader.describe(args.dataset))
            elif args.command == "research-read":
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
                emit({"database": str(store.path), "schema_version": 3})
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
    except ValidationError as exc:
        emit({"error": {"code": "INVALID_INPUT", "message": str(exc)}})
        return 2
    except Exception as exc:
        emit({"error": {"code": "UNEXPECTED_ERROR", "type": type(exc).__name__}})
        return 1


if __name__ == "__main__":
    sys.exit(main())
