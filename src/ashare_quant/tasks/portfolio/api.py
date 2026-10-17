"""Workbench API; the legacy v6 service remains a separate contract."""

from fastapi import Request, Response

from ashare_quant.backtests.daily.contracts import Sha
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import digest
from ashare_quant.tasks.daily.api import ActionRequest
from ashare_quant.tasks.daily.contracts import JobID, JobLogs
from ashare_quant.tasks.portfolio.contracts import (
    PortfolioCatalogue,
    PortfolioCreate,
    PortfolioJobList,
    PortfolioJobView,
    PortfolioManifest,
    PortfolioPreflight,
    PortfolioResult,
    PortfolioSelection,
    PortfolioServiceStatus,
    PortfolioStrategyDefinition,
    PortfolioStrategyVersion,
)
from ashare_quant.tasks.portfolio.service import PortfolioJobs


def mount(app, store, configuration=None):
    jobs = PortfolioJobs(store, configuration)
    app.state.portfolio_jobs = jobs
    prefix = "/api/v1/portfolio"

    def closed(request):
        if request.query_params:
            raise DomainError("PORTFOLIO_QUERY_UNSUPPORTED", "Use the typed request body", 422)

    @app.get(prefix + "/catalogue")
    def catalogue(request: Request) -> PortfolioCatalogue:
        closed(request)
        return jobs.catalogue()

    @app.get(prefix + "/openapi.json", include_in_schema=False)
    def openapi(request: Request) -> dict:
        from ashare_quant.tasks.openapi import portfolio_document

        closed(request)
        return portfolio_document(app)

    @app.post(prefix + "/preflight")
    def preflight(body: PortfolioSelection, request: Request) -> PortfolioPreflight:
        closed(request)
        return jobs.preflight(body)

    @app.post(prefix + "/strategies", status_code=201)
    def register(body: PortfolioStrategyDefinition, request: Request) -> PortfolioStrategyVersion:
        closed(request)
        return jobs.register_strategy(body)

    @app.get(prefix + "/strategies")
    def strategies(request: Request) -> tuple[PortfolioStrategyVersion, ...]:
        closed(request)
        jobs.guard()
        return jobs.strategies.list()

    @app.get(prefix + "/strategies/{version_id}")
    def strategy(version_id: Sha, request: Request) -> PortfolioStrategyVersion:
        closed(request)
        jobs.guard()
        return jobs.strategies.get(version_id)

    @app.get(prefix + "/service")
    def service(request: Request) -> PortfolioServiceStatus:
        closed(request)
        return jobs.service_status()

    @app.post(prefix + "/jobs", status_code=201)
    def create(body: PortfolioCreate, request: Request) -> PortfolioJobView:
        closed(request)
        return jobs.create(body)

    @app.get(prefix + "/jobs")
    def listing(request: Request) -> PortfolioJobList:
        closed(request)
        return jobs.list()

    @app.get(prefix + "/jobs/{job_id}")
    def get(job_id: JobID, request: Request) -> PortfolioJobView:
        closed(request)
        return jobs.get(job_id)

    @app.get(prefix + "/jobs/{job_id}/manifest")
    def manifest(job_id: JobID, request: Request) -> PortfolioManifest:
        closed(request)
        return jobs.check(jobs.repo.row(job_id))

    @app.get(prefix + "/jobs/{job_id}/logs")
    def logs(job_id: JobID, request: Request) -> JobLogs:
        closed(request)
        return jobs.logs(job_id)

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(
        job_id: JobID, request: Request, body: ActionRequest = ActionRequest()
    ) -> PortfolioJobView:
        closed(request)
        return jobs.cancel(job_id)

    @app.post(prefix + "/jobs/{job_id}/resume")
    def resume(
        job_id: JobID, request: Request, body: ActionRequest = ActionRequest()
    ) -> PortfolioJobView:
        closed(request)
        return jobs.resume(job_id)

    @app.get(prefix + "/jobs/{job_id}/result")
    def result(job_id: JobID, request: Request, response: Response) -> PortfolioResult:
        closed(request)
        value = jobs.result(job_id)
        response.headers["ETag"] = '"' + digest(value) + '"'
        return value
