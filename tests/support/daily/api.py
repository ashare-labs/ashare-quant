"""Local task control endpoints, separate from the closed general backtest gate."""

from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ashare_quant.contracts import Contract, DomainError
from ashare_quant.storage.artifacts import digest
from ashare_quant.tasks.policy import ServiceStatus
from tests.support.daily.contracts import (
    DataBindRequest,
    JobCatalogue,
    JobCreate,
    JobID,
    JobList,
    JobLogs,
    JobManifest,
    JobResult,
    JobView,
)
from tests.support.daily.service import Jobs
from tests.support.legacy_backtests.contracts import Sha, StrategyVersion
from tests.support.legacy_strategies.rules import RuleStrategyDefinition


class ActionRequest(Contract):
    pass


def mount(app, store, configuration=None, *, jobs=None):
    jobs = jobs or Jobs(store, configuration)
    app.state.daily_tasks = jobs

    @app.middleware("http")
    async def task_no_store(request, call_next):
        if request.url.path.startswith(("/api/v1/tasks/daily/", "/api/v1/portfolio/", "/api/v1/python/")) and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            try:
                parsed = urlsplit(origin) if origin else None
            except ValueError:
                parsed = None
            trusted = (not origin and request.headers.get("sec-fetch-site") != "cross-site") or (
                parsed and parsed.scheme in {"http", "https"}
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username and not parsed.password
                and parsed.path == "" and not parsed.query and not parsed.fragment
            )
            if not trusted:
                return JSONResponse(status_code=403, content={"api_version": "v1", "error": {
                    "code": "M2_JOB_ORIGIN_REFUSED",
                    "message": "Mutations require a trusted local client or loopback browser origin",
                    "details": None,
                }}, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        if response.status_code in {429, 503}:
            response.headers["Retry-After"] = "2"
        if request.url.path.startswith(("/api/v1/tasks/daily/", "/api/v1/portfolio/", "/api/v1/python/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def closed_query(request):
        if request.query_params:
            raise DomainError(
                "M2_JOB_QUERY_NOT_SUPPORTED",
                "Finite task endpoints accept only their typed input",
                422,
            )

    @app.get("/api/v1/tasks/daily/openapi.json", include_in_schema=False)
    def scoped_openapi(request: Request) -> dict:
        from tests.support.openapi import document

        closed_query(request)
        return document(app)

    @app.post("/api/v1/tasks/daily/strategies", status_code=201)
    def register_strategy(body: RuleStrategyDefinition, request: Request) -> StrategyVersion:
        closed_query(request)
        return jobs.register_strategy(body)

    @app.get("/api/v1/tasks/daily/strategies")
    def list_strategies(request: Request) -> tuple[StrategyVersion, ...]:
        closed_query(request)
        return jobs.list_strategies()

    @app.get("/api/v1/tasks/daily/strategies/{version_id}")
    def get_strategy(version_id: Sha, request: Request) -> StrategyVersion:
        closed_query(request)
        return jobs.get_strategy(version_id)

    @app.get("/api/v1/tasks/daily/python-strategies")
    def python_strategies(request: Request) -> tuple[StrategyVersion, ...]:
        closed_query(request)
        return jobs.python_strategies()

    @app.get("/api/v1/tasks/daily/python-strategies/{version_id}")
    def python_strategy(version_id: Sha, request: Request) -> StrategyVersion:
        closed_query(request)
        return jobs.python_strategy(version_id)

    @app.post("/api/v1/tasks/daily/data/bind")
    def bind(body: DataBindRequest, request: Request) -> JobCatalogue:
        closed_query(request)
        return jobs.bind(body.dataset_id)

    @app.get("/api/v1/tasks/daily/jobs/catalogue")
    def catalogue(request: Request) -> JobCatalogue:
        closed_query(request)
        return jobs.catalogue()

    @app.post("/api/v1/tasks/daily/jobs", status_code=201)
    def create(body: JobCreate, request: Request) -> JobView:
        closed_query(request)
        return jobs.create(body)

    @app.get("/api/v1/tasks/daily/jobs")
    def listing(request: Request) -> JobList:
        closed_query(request)
        return jobs.list()

    @app.get("/api/v1/tasks/daily/jobs/service")
    def service_status(request: Request) -> ServiceStatus:
        closed_query(request)
        return jobs.service_status()

    @app.get("/api/v1/tasks/daily/jobs/{job_id}")
    def get(job_id: JobID, request: Request) -> JobView:
        closed_query(request)
        return jobs.get(job_id)

    @app.get("/api/v1/tasks/daily/jobs/{job_id}/manifest")
    def manifest(job_id: JobID, request: Request) -> JobManifest:
        closed_query(request)
        return jobs.check(jobs.repo.row(job_id))

    @app.get("/api/v1/tasks/daily/jobs/{job_id}/logs")
    def logs(job_id: JobID, request: Request) -> JobLogs:
        closed_query(request)
        return jobs.logs(job_id)

    @app.post("/api/v1/tasks/daily/jobs/{job_id}/cancel")
    def cancel(job_id: JobID, request: Request, body: ActionRequest = ActionRequest()) -> JobView:
        closed_query(request)
        return jobs.cancel(job_id)

    @app.post("/api/v1/tasks/daily/jobs/{job_id}/resume")
    def resume(job_id: JobID, request: Request, body: ActionRequest = ActionRequest()) -> JobView:
        closed_query(request)
        return jobs.resume(job_id)

    @app.get("/api/v1/tasks/daily/jobs/{job_id}/result")
    def result(job_id: JobID, request: Request, response: Response) -> JobResult:
        closed_query(request)
        value = jobs.result(job_id)
        response.headers["ETag"] = '"' + digest(value) + '"'
        return value
