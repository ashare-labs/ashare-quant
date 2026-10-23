"""Local trusted Python strategy workbench, sharing the existing worker."""

from fastapi import Request

from ashare_quant.contracts import DomainError
from ashare_quant.strategies.files_api import mount as mount_files
from ashare_quant.tasks.api import ActionRequest
from ashare_quant.tasks.contracts import JobID, JobLogs
from ashare_quant.tasks.minute.contracts import (
    PythonCreate,
    PythonJobList,
    PythonJobView,
    PythonManifest,
    PythonResult,
)
from ashare_quant.tasks.minute.service import PythonJobs


def mount(app, store, configuration=None):
    jobs = PythonJobs(store, configuration, app.state.product)
    app.state.python_jobs = jobs
    mount_files(app, app.state.product)
    prefix = "/api/v1/python"

    def closed(request):
        if request.query_params:
            raise DomainError("PYTHON_QUERY_UNSUPPORTED", "Use the typed request body", 422)

    @app.get(prefix + "/catalogue")
    def catalogue(request: Request) -> dict:
        closed(request)
        factors = jobs.history_factors
        return {
            "environment_id": jobs.environment(),
            "frequency": "1m",
            "frequencies": ["1m", "1d"],
            "daily": {
                "history": "requested_at_runtime",
                "terminal_positions": "marked_at_final_close",
                "history_factors": factors["description"] if factors else None,
            },
            "resource_budget": jobs.repo.policy.model_dump(mode="json"),
            "terminal_positions": "marked_at_final_close",
            "historical_pit": False,
            "universe": "strategy_subscription",
            "external_securities_required": False,
            "catalogue_semantics": "retrospective recorded listing/code intervals; not historical PIT",

        }

    @app.post(prefix + "/jobs", status_code=201)
    def create(body: PythonCreate, request: Request) -> PythonJobView:
        closed(request)
        return jobs.create(body)

    @app.get(prefix + "/jobs")
    def listing(request: Request) -> PythonJobList:
        closed(request)
        return jobs.list()

    @app.get(prefix + "/jobs/{job_id}")
    def get(job_id: JobID) -> PythonJobView:
        return jobs.get(job_id)

    @app.get(prefix + "/jobs/{job_id}/manifest")
    def manifest(job_id: JobID) -> PythonManifest:
        return jobs.check(jobs.repo.row(job_id))

    @app.get(prefix + "/jobs/{job_id}/logs")
    def logs(job_id: JobID) -> JobLogs:
        return jobs.logs(job_id)

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(job_id: JobID, body: ActionRequest = ActionRequest()) -> PythonJobView:
        return jobs.cancel(job_id)

    @app.post(prefix + "/jobs/{job_id}/resume")
    def resume(job_id: JobID, body: ActionRequest = ActionRequest()) -> PythonJobView:
        return jobs.resume(job_id)

    @app.get(prefix + "/jobs/{job_id}/result")
    def result(job_id: JobID) -> PythonResult:
        return jobs.result(job_id)
