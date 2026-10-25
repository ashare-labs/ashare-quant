"""Shared controls and read-only historical evidence; no predefined strategies."""

from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse

from ashare_quant.access import trusted_browser_request
from ashare_quant.contracts import Contract, DomainError
from ashare_quant.tasks.analysis_contracts import (
    AnalysisVersion,
    BenchmarkSelection,
    ResultAnalysis,
)
from ashare_quant.tasks.benchmark_comparison import check_query
from ashare_quant.tasks.contracts import JobID, JobLogs
from ashare_quant.tasks.history import History
from ashare_quant.tasks.policy import ServiceStatus
from ashare_quant.tasks.service import Jobs


class ActionRequest(Contract):
    pass


def mount(app, store, *, jobs=None):
    app.state.tasks = jobs or Jobs(store)
    history = History(store, app.state.tasks.config)
    app.state.history = history

    @app.middleware("http")
    async def local_tasks(request, call_next):
        scoped = request.url.path.startswith(("/api/v1/tasks/", "/api/v1/python/", "/api/v1/factors/", "/api/v1/history/"))
        if scoped and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not trusted_browser_request(request):
                return JSONResponse(status_code=403, content={"api_version": "v1", "error": {
                    "code": "M2_JOB_ORIGIN_REFUSED", "message": "Use a trusted local client",
                    "details": None}}, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        if scoped:
            response.headers["Cache-Control"] = "no-store"
        if response.status_code in {429, 503}:
            response.headers["Retry-After"] = "2"
        return response

    @app.get("/api/v1/tasks/openapi.json", include_in_schema=False)
    def schema():
        from ashare_quant.tasks.openapi import document

        return document(app)

    @app.get("/api/v1/tasks/service")
    def service(request: Request) -> ServiceStatus:
        if request.query_params:
            raise DomainError("M2_JOB_QUERY_UNSUPPORTED", "Service policy is configured by the operator", 422)
        return app.state.tasks.service_status()

    @app.get("/api/v1/history/jobs")
    def listing(family: Literal["daily", "portfolio", "python", "factors"] | None = None) -> dict:
        return history.list(family)

    @app.get("/api/v1/history/strategies")
    def strategies() -> dict:
        return history.strategies()

    @app.get("/api/v1/history/jobs/{job_id}")
    def get(job_id: JobID) -> dict:
        return history.get(job_id)

    @app.get("/api/v1/history/jobs/{job_id}/manifest")
    def manifest(job_id: JobID) -> dict:
        return history.check(history.repo.row(job_id))

    @app.get("/api/v1/history/jobs/{job_id}/logs")
    def logs(job_id: JobID) -> JobLogs:
        return history.logs(job_id)

    @app.get("/api/v1/history/jobs/{job_id}/result")
    def result(job_id: JobID) -> dict:
        return history.result_response(job_id)

    @app.get("/api/v1/history/jobs/{job_id}/analysis", include_in_schema=False, response_model_exclude_unset=True)
    @app.get("/api/v1/history/jobs/{job_id}/result/analysis", response_model_exclude_unset=True)
    def analysis(job_id: JobID, request: Request, analysis_version: AnalysisVersion = "returns-v1",
                 benchmark: BenchmarkSelection = "frozen") -> ResultAnalysis:
        check_query(request, analysis_version, benchmark)
        return history.analysis(job_id, analysis_version=analysis_version, benchmark=benchmark)
