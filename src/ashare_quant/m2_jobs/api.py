"""Local task control endpoints, separate from the closed general backtest gate."""

from fastapi import Request, Response

from ashare_quant.contracts import Contract, DomainError

from .contracts import (
    JobCatalogue,
    JobCreate,
    JobID,
    JobList,
    JobLogs,
    JobManifest,
    JobResult,
    JobView,
)
from .files import digest
from .service import Jobs


class ActionRequest(Contract):
    pass


def mount(app, store, configuration=None):
    jobs = Jobs(store, configuration)
    app.state.m2_jobs = jobs

    @app.middleware("http")
    async def task_no_store(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/m2/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def closed_query(request):
        if request.query_params:
            raise DomainError(
                "M2_JOB_QUERY_NOT_SUPPORTED",
                "Finite task endpoints accept only their typed input",
                422,
            )

    @app.get("/api/v1/m2/openapi.json", include_in_schema=False)
    def scoped_openapi(request: Request) -> dict:
        from .openapi import document

        closed_query(request)
        return document(app)

    @app.get("/api/v1/m2/jobs/catalogue")
    def catalogue(request: Request) -> JobCatalogue:
        closed_query(request)
        return jobs.catalogue()

    @app.post("/api/v1/m2/jobs", status_code=201)
    def create(body: JobCreate, request: Request) -> JobView:
        closed_query(request)
        return jobs.create(body)

    @app.get("/api/v1/m2/jobs")
    def listing(request: Request) -> JobList:
        closed_query(request)
        return jobs.list()

    @app.get("/api/v1/m2/jobs/{job_id}")
    def get(job_id: JobID, request: Request) -> JobView:
        closed_query(request)
        return jobs.get(job_id)

    @app.get("/api/v1/m2/jobs/{job_id}/manifest")
    def manifest(job_id: JobID, request: Request) -> JobManifest:
        closed_query(request)
        return jobs.check(jobs.repo.row(job_id))

    @app.get("/api/v1/m2/jobs/{job_id}/logs")
    def logs(job_id: JobID, request: Request) -> JobLogs:
        closed_query(request)
        return jobs.logs(job_id)

    @app.post("/api/v1/m2/jobs/{job_id}/cancel")
    def cancel(job_id: JobID, request: Request, body: ActionRequest = ActionRequest()) -> JobView:
        closed_query(request)
        return jobs.cancel(job_id)

    @app.post("/api/v1/m2/jobs/{job_id}/resume")
    def resume(job_id: JobID, request: Request, body: ActionRequest = ActionRequest()) -> JobView:
        closed_query(request)
        return jobs.resume(job_id)

    @app.get("/api/v1/m2/jobs/{job_id}/result")
    def result(job_id: JobID, request: Request, response: Response) -> JobResult:
        closed_query(request)
        value = jobs.result(job_id)
        response.headers["ETag"] = '"' + digest(value) + '"'
        return value
