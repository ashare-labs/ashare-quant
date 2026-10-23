"""Discovery, immutable research requests and read-only result restoration."""

from ashare_data.factors.alpha191 import EXPLICIT_VERSION

from ashare_quant.tasks.api import ActionRequest
from ashare_quant.tasks.contracts import JobID, JobLogs
from ashare_quant.tasks.factors.contracts import (
    FactorCreate,
    FactorJobList,
    FactorJobView,
    FactorResult,
)
from ashare_quant.tasks.factors.service import FactorJobs


def mount(app, store, configuration=None, sources=None):
    jobs = FactorJobs(store, configuration, sources, app.state.product)
    app.state.factor_jobs = jobs
    prefix = "/api/v1/factors"

    @app.get(prefix + "/catalogue")
    def catalogue(version: str = EXPLICIT_VERSION) -> dict:
        return jobs.catalogue(version)

    @app.get(prefix + "/sources")
    def source_list() -> dict:
        return {"items": jobs.sources.catalogue(), "environment_id": jobs.environment()}

    @app.post(prefix + "/jobs", status_code=201)
    def create(body: FactorCreate) -> FactorJobView:
        return jobs.create(body)

    @app.get(prefix + "/jobs")
    def listing() -> FactorJobList:
        return jobs.list()

    @app.get(prefix + "/jobs/{job_id}")
    def get(job_id: JobID) -> FactorJobView:
        return jobs.get(job_id)

    @app.get(prefix + "/jobs/{job_id}/logs")
    def logs(job_id: JobID) -> JobLogs:
        return jobs.logs(job_id)

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(job_id: JobID, body: ActionRequest = ActionRequest()) -> FactorJobView:
        return jobs.cancel(job_id)

    @app.get(prefix + "/jobs/{job_id}/result")
    def result(job_id: JobID) -> FactorResult:
        return jobs.result(job_id)
