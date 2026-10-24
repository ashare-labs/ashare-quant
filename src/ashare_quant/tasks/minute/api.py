"""Local trusted Python strategy workbench, sharing the existing worker."""

from fastapi import Request

from ashare_quant.contracts import DomainError
from ashare_quant.strategies.backtests import mount as mount_backtests
from ashare_quant.strategies.files_api import mount as mount_files
from ashare_quant.tasks.analysis_contracts import ResultAnalysis
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
    mount_backtests(app)
    prefix = "/api/v1/python"

    def closed(request):
        if request.query_params:
            raise DomainError("PYTHON_QUERY_UNSUPPORTED", "Use the typed request body", 422)

    @app.get(prefix + "/catalogue")
    def catalogue(request: Request) -> dict:
        closed(request)
        factors = jobs.history_factors
        environment = jobs.environment()
        from ashare_quant.tasks.minute.benchmark import configured_release
        benchmark = configured_release(jobs.product.configured())

        def availability(owner):
            release = getattr(owner, "release", None)
            if release is None:
                return None
            with release.metadata() as db:
                first = [r[0] for r in db.execute("SELECT date FROM calendar ORDER BY date LIMIT 2")]
                last = [r[0] for r in db.execute("SELECT date FROM calendar ORDER BY date DESC LIMIT 2")]
            return {
                "release_id": release.release_id, "manifest_sha256": release.sha256,
                "calendar_start": first[0] if first else None,
                "calendar_end": last[0] if last else None,
                "first_execution_session": first[1] if len(first) > 1 else None,
                "last_execution_session": last[1] if len(last) > 1 else None,
                "coverage_semantics": "calendar envelope only; security, field and history coverage checked on actual demand",
            }

        return {
            "environment_id": environment,
            "strategy_api": {"module": "aquant", "distribution": "ashare-quant",
                             "binding": "runtime_only", "legacy_module_supported": False},
            "strategy_project": {
                "descriptor": "aquant.toml", "format_version": 1,
                "default_entrypoint": "main.py", "identity": "stable_project_id",
                "source_files": "relative_python_paths", "imports": "package_relative",
                "submission": "complete_snapshot", "dependencies": "existing_runtime_environment",
            },
            "data_availability": jobs.product.call(availability),
            "frequency": "1m",
            "frequencies": ["1m", "1d"],
            "benchmark": {
                "selection": "set_benchmark_in_initialize",
                "supplemental_release": benchmark,
                "price_basis": "published_close_price_return",
                "missing_reference": "strategy_result_retained; comparison_unavailable",
            },
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
        jobs.check(jobs.repo.row(job_id))
        return app.state.history.result_response(job_id)

    @app.get(prefix + "/jobs/{job_id}/analysis", include_in_schema=False)
    @app.get(prefix + "/jobs/{job_id}/result/analysis")
    def analysis(job_id: JobID) -> ResultAnalysis:
        return app.state.history.analysis(job_id)
