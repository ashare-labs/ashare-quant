"""Local trusted Python strategy workbench, sharing the existing worker."""

from fastapi import Request

from ashare_quant.contracts import DomainError
from ashare_quant.tasks.daily.api import ActionRequest
from ashare_quant.tasks.daily.contracts import JobID, JobLogs
from ashare_quant.tasks.minute.contracts import (
    PythonCreate,
    PythonJobList,
    PythonJobView,
    PythonResult,
)
from ashare_quant.tasks.minute.example import SOURCE
from ashare_quant.tasks.minute.service import PythonJobs


def mount(app, store, configuration=None):
    jobs = PythonJobs(store, configuration, app.state.product)
    app.state.python_jobs = jobs
    prefix = "/api/v1/python"

    def closed(request):
        if request.query_params:
            raise DomainError("PYTHON_QUERY_UNSUPPORTED", "Use the typed request body", 422)

    @app.get(prefix + "/catalogue")
    def catalogue(request: Request) -> dict:
        closed(request)
        return {
            "environment_id": jobs.environment(),
            "example": SOURCE,
            "frequency": "1m",
            "max_securities": 8,
            "max_sessions": 23,
            "max_stock_minutes": 20000,
            "terminal_positions": "flat",
            "historical_pit": False,
            "universe": "fixed_at_submission",
            "parameters": {"lookback": 5, "shares": 100},
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

    @app.get(prefix + "/jobs/{job_id}/logs")
    def logs(job_id: JobID) -> JobLogs:
        return jobs.logs(job_id)

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(job_id: JobID, body: ActionRequest = ActionRequest()) -> PythonJobView:
        return jobs.cancel(job_id)

    @app.get(prefix + "/jobs/{job_id}/result")
    def result(job_id: JobID) -> PythonResult:
        return jobs.result(job_id)
