"""Local HTTP facade with versioned OpenAPI and typed response projections."""

import json
import os
import sqlite3
from typing import Annotated

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .contracts import (
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
from .d1 import (
    D1Check,
    D1CheckRequest,
    IntentPreview,
    PlanView,
    PreviewRequest,
    describe_plan,
    preflight,
    preview,
    refuse_execution,
)
from .d1_br1 import (
    BR1Check,
    BR1ProfileView,
    BR1Request,
    br1_preflight,
    describe_br1_profile,
    refuse_br1_execution,
)
from .d1_codec import BR1BindRequest, FrozenBindingReport, FrozenBR1Reader
from .d1_result_contracts import BR1ResultDetail, BR1ResultList
from .d1_results import canonical, digest, list_results, result_detail
from .data import PinnedAshareData, capabilities
from .engine import specifications
from .research import (
    ResearchCoverage,
    ResearchCoverageQuery,
    ResearchDescription,
    ResearchQuery,
    ResearchRead,
    ResearchReader,
)
from .rq_specs import rq_specifications
from .service import Service
from .store import Store


def create_app(db_path: str | None = None, research_stores: dict[str, str] | None = None, m2_jobs_configuration=None, diagnostics_configuration=None) -> FastAPI:
    app = FastAPI(title="A宽 backend", version=__version__,
                  openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", redoc_url=None,
                  responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                             409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                             503: {"model": ErrorResponse},
                             422: {"model": ErrorResponse | ValidationResponse}})
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    try:
        store = Store(db_path or os.getenv("ASHARE_QUANT_DB", ".state/control.sqlite3"))
    except DomainError as exc:
        app.state.database_error = exc.body()

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                       status_code=409, response_model=ErrorResponse)
        def incompatible_database(path: str):
            return JSONResponse(status_code=409, content={
                "error": app.state.database_error, "api_version": "v1",
            }, headers={"Cache-Control": "no-store"})

        return app
    service = Service(store)
    app.state.store = store
    from .m2.api import mount as mount_m2
    mount_m2(app, store)
    from .m2_results.api import mount as mount_m2_results
    mount_m2_results(app)
    from .m2_jobs.api import mount as mount_m2_jobs
    mount_m2_jobs(app, store, m2_jobs_configuration)
    from .diagnostics.api import mount as mount_diagnostics
    mount_diagnostics(app, diagnostics_configuration)
    roots = dict(research_stores) if research_stores is not None else json.loads(os.getenv("ASHARE_QUANT_RESEARCH_STORES", "{}"))
    if not isinstance(roots, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in roots.items()):
        raise ValueError("ASHARE_QUANT_RESEARCH_STORES must map explicit aliases to store paths")

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
    async def storage_error(_, exc):
        busy = getattr(exc, "sqlite_errorcode", 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        return JSONResponse(status_code=503 if busy else 409, content={"api_version": "v1", "error": {
            "code": "DATABASE_BUSY" if busy else "DATABASE_OPERATION_REFUSED",
            "message": "Database busy; retry later" if busy else "Database operation refused; preserve the database for inspection",
            "details": None,
        }}, headers={"Retry-After": "2", "Cache-Control": "no-store"})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "api_version": "v1", "backend_version": __version__}

    @app.get("/api/v1/capabilities")
    def get_capabilities() -> Capabilities:
        return capabilities()

    @app.get("/api/v1/d1/plan")
    def d1_plan() -> PlanView:
        return describe_plan()

    @app.post("/api/v1/d1/preflight")
    def d1_preflight(request: D1CheckRequest) -> D1Check:
        return preflight(request)

    @app.post("/api/v1/d1/intent-preview")
    def d1_preview(request: PreviewRequest) -> IntentPreview:
        return preview(request)

    @app.post("/api/v1/d1/runs")
    def d1_run(request: D1CheckRequest) -> ErrorResponse:
        preflight(request)
        refuse_execution()

    def result_request(request: Request):
        if request.query_params:
            raise DomainError("BR1_RESULT_QUERY_NOT_SUPPORTED", "Result reads accept only the sealed result ID", 422)

    def result_headers(response: Response, value):
        response.headers["ETag"] = '"' + digest(canonical(value.model_dump(mode="json"))) + '"'
        response.headers["Cache-Control"] = "no-cache"

    @app.get("/api/v1/d1-br1/results", responses={503: {"model": ErrorResponse}})
    def br1_results(request: Request, response: Response) -> BR1ResultList:
        result_request(request)
        value = list_results()
        result_headers(response, value)
        return value

    @app.get("/api/v1/d1-br1/results/{result_id}", responses={503: {"model": ErrorResponse}})
    def br1_result(result_id: str, request: Request, response: Response) -> BR1ResultDetail:
        result_request(request)
        value = result_detail(result_id)
        result_headers(response, value)
        return value

    @app.get("/api/v1/d1-br1/profile")
    def br1_profile() -> BR1ProfileView:
        return describe_br1_profile()

    @app.post("/api/v1/d1-br1/preflight")
    def br1_check(request: BR1Request) -> BR1Check:
        return br1_preflight(request)

    @app.post("/api/v1/d1-br1/runs")
    def br1_run(request: BR1Request) -> ErrorResponse:
        br1_preflight(request)
        refuse_br1_execution()

    @app.post("/api/v1/d1-br1/bind/{store_alias}/{dataset_id}")
    def br1_bind(store_alias: str, dataset_id: str, request: BR1BindRequest) -> FrozenBindingReport:
        if store_alias not in roots:
            raise DomainError("RESEARCH_STORE_NOT_CONFIGURED", "Configure an explicit local owner store alias", 422)
        return FrozenBR1Reader(roots[store_alias], dataset_id=dataset_id, mode=request.mode,
                               backend_profile_sha256=request.backend_profile_sha256).snapshot().report

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

    @app.get("/api/v1/research/stores")
    def research_aliases() -> list[str]:
        return sorted(roots)

    @app.get("/api/v1/research/{store_alias}/{dataset_id}")
    def research_describe(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]) -> ResearchDescription:
        return research_reader(store_alias).describe(dataset_id)

    @app.post("/api/v1/research/{store_alias}/{dataset_id}/prices")
    def research_prices(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")], request: ResearchQuery) -> ResearchRead:
        return research_reader(store_alias).read(dataset_id, request)

    @app.post("/api/v1/research/{store_alias}/{dataset_id}/coverage")
    def research_coverage(store_alias: str, dataset_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")], request: ResearchCoverageQuery) -> ResearchCoverage:
        return research_reader(store_alias).coverage(dataset_id, request)

    return app
