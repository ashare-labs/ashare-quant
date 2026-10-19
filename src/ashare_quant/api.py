"""Local HTTP facade with versioned OpenAPI and typed response projections."""

import json
import os
import sqlite3
from typing import Annotated

from fastapi import FastAPI, Path, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from ashare_quant import __version__
from ashare_quant.backtests.synthetic import specifications
from ashare_quant.contracts import (
    Account,
    Capabilities,
    DataBatch,
    DataQuery,
    DomainError,
    ErrorResponse,
    EventPage,
    ExecutionStatus,
    Fill,
    LogEntry,
    Manifest,
    Order,
    RunCreate,
    RunView,
    StateView,
    StrategyCreate,
    StrategyVersion,
    ValidationResponse,
)
from ashare_quant.data import PinnedAshareData, capabilities
from ashare_quant.execution.synthetic.specs import rq_specifications
from ashare_quant.market.reader import (
    ResearchCoverage,
    ResearchCoverageQuery,
    ResearchDescription,
    ResearchQuery,
    ResearchRead,
    ResearchReader,
)
from ashare_quant.storage.errors import storage_error
from ashare_quant.storage.store import Store
from ashare_quant.tasks.synthetic.service import Service


def create_app(db_path: str | None = None, market_stores: dict[str, str] | None = None, daily_tasks_configuration=None, diagnostics_configuration=None, product_configuration=None) -> FastAPI:
    app = FastAPI(title="A宽 backend", version=__version__,
                  openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", redoc_url=None,
                  responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                             409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                             503: {"model": ErrorResponse},
                             422: {"model": ErrorResponse | ValidationResponse}})
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    try:
        from ashare_quant.deployment.configuration import deployment_path

        if product_configuration is None and deployment_path() is not None:
            from ashare_quant.product.service import environment_configuration

            product_configuration = environment_configuration()
        if product_configuration is not None:
            from pathlib import Path as FilePath

            from ashare_quant.deployment.runtime import require_initialized
            from ashare_quant.tasks.daily.executor import Configuration

            db_path = db_path if db_path is not None else str(product_configuration.control_db)
            if FilePath(db_path).expanduser().resolve() != product_configuration.control_db:
                raise DomainError(
                    "PRODUCT_STATE_MISMATCH",
                    "Start the API and task worker with the same deployment state database", 409,
                )
            require_initialized(product_configuration)
            if daily_tasks_configuration is None:
                daily_tasks_configuration = Configuration(product_configuration.owner_store_root)
        store = Store(db_path or os.getenv("ASHARE_QUANT_DB", ".state/control.sqlite3"))
        from ashare_quant.tasks.daily.service import Jobs

        jobs = Jobs(store, daily_tasks_configuration)
    except (DomainError, OSError, sqlite3.Error) as exc:
        error = exc if isinstance(exc, DomainError) else storage_error(exc)
        app.state.database_error = error.body()
        app.state.database_error_status = error.status

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                       status_code=409, response_model=ErrorResponse)
        def incompatible_database(path: str):
            return JSONResponse(status_code=app.state.database_error_status, content={
                "error": app.state.database_error, "api_version": "v1",
            }, headers={"Cache-Control": "no-store", "Retry-After": "2"})

        return app
    service = Service(store)
    app.state.store = store
    from ashare_quant.tasks.daily.api import mount as mount_tasks
    mount_tasks(app, store, daily_tasks_configuration, jobs=jobs)
    from ashare_quant.tasks.portfolio.api import mount as mount_portfolio_jobs
    mount_portfolio_jobs(app, store, daily_tasks_configuration)
    from ashare_quant.product.api import mount as mount_product
    mount_product(app, store, product_configuration)
    from ashare_quant.diagnostics.api import mount as mount_diagnostics
    mount_diagnostics(app, diagnostics_configuration)
    roots = dict(market_stores) if market_stores is not None else json.loads(os.getenv("ASHARE_QUANT_MARKET_STORES", "{}"))
    if not isinstance(roots, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in roots.items()):
        raise ValueError("ASHARE_QUANT_MARKET_STORES must map explicit aliases to store paths")

    def research_reader(alias):
        if alias not in roots:
            raise DomainError("RESEARCH_STORE_NOT_CONFIGURED", "Configure an explicit data-owner store alias on this local backend", 422)
        return ResearchReader(roots[alias])

    @app.exception_handler(DomainError)
    async def domain_error(_, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={
            "error": exc.body(), "api_version": "v1",
        })

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def local_storage_error(_, exc):
        error = storage_error(exc)
        return JSONResponse(status_code=error.status, content={"api_version": "v1", "error": error.body()},
                            headers={"Retry-After": "2", "Cache-Control": "no-store"})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "api_version": "v1", "backend_version": __version__}

    @app.get("/api/v1/capabilities")
    def get_capabilities() -> Capabilities:
        return capabilities()

    @app.get("/api/v1/scenarios")
    def scenarios() -> dict:
        return specifications()

    @app.get("/api/v1/rqalpha/scenarios")
    def native_scenarios() -> dict:
        return rq_specifications()

    @app.get("/api/v1/runs/{run_id}/receipt")
    def native_receipt(run_id: str) -> dict:
        return store.receipt(run_id)

    @app.get("/api/v1/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)) -> EventPage:
        return store.events(run_id, after, limit)

    @app.get("/api/v1/runs/{run_id}/execution")
    def execution(run_id: str) -> ExecutionStatus:
        return store.execution_status(run_id)

    @app.post("/api/v1/strategies", status_code=201)
    def register_strategy(request: StrategyCreate) -> StrategyVersion:
        return service.register_strategy(request)

    @app.get("/api/v1/strategies")
    def list_strategies() -> list[StrategyVersion]:
        return store.strategies()

    @app.get("/api/v1/strategies/{version_id}")
    def strategy(version_id: str) -> StrategyVersion:
        return store.strategy(version_id)

    @app.post("/api/v1/runs", status_code=202)
    def submit_run(request: RunCreate) -> RunView:
        return service.submit(request)

    @app.get("/api/v1/runs")
    def list_runs() -> list[RunView]:
        return store.runs()

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str) -> RunView:
        return store.run(run_id)

    @app.get("/api/v1/runs/{run_id}/manifest")
    def manifest(run_id: str) -> Manifest:
        return store.manifest(run_id)

    @app.get("/api/v1/runs/{run_id}/state")
    def state(run_id: str) -> StateView:
        return store.state(run_id)

    @app.get("/api/v1/runs/{run_id}/orders")
    def orders(run_id: str) -> list[Order]:
        return store.state(run_id).orders

    @app.get("/api/v1/runs/{run_id}/fills")
    def fills(run_id: str) -> list[Fill]:
        return store.state(run_id).fills

    @app.get("/api/v1/runs/{run_id}/account")
    def account(run_id: str) -> Account | None:
        return store.state(run_id).account

    @app.get("/api/v1/runs/{run_id}/logs")
    def logs(run_id: str, after: int = Query(default=0, ge=0)) -> list[LogEntry]:
        return store.logs(run_id, after)

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel(run_id: str) -> RunView:
        return store.cancel(run_id)

    @app.post("/api/v1/data/admission")
    def admission(query: DataQuery) -> DataBatch:
        return PinnedAshareData().read(query)

    @app.get("/api/v1/market/stores")
    def research_aliases() -> list[str]:
        return sorted(roots)

    @app.get("/api/v1/market/{store_alias}/{dataset_id}")
    def research_describe(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]) -> ResearchDescription:
        return research_reader(store_alias).describe(dataset_id)

    @app.post("/api/v1/market/{store_alias}/{dataset_id}/prices")
    def research_prices(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")], request: ResearchQuery) -> ResearchRead:
        return research_reader(store_alias).read(dataset_id, request)

    @app.post("/api/v1/market/{store_alias}/{dataset_id}/coverage")
    def research_coverage(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")], request: ResearchCoverageQuery) -> ResearchCoverage:
        return research_reader(store_alias).coverage(dataset_id, request)

    return app
