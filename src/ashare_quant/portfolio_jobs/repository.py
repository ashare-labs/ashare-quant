"""Typed portfolio rows using the existing queue, leases, logs and SQL transitions."""

from ashare_quant.m2_jobs.files import canonical, digest, require
from ashare_quant.m2_jobs.repository import Repository as TaskRepository

from .contracts import PortfolioCreate, PortfolioJobView, PortfolioManifest


class Repository(TaskRepository):
    view_type = PortfolioJobView

    @staticmethod
    def manifest(row):
        value = PortfolioManifest.model_validate_json(row["manifest"])
        require(
            digest(value) == row["manifest_sha256"]
            and value.job_id == row["job_id"]
            and digest(value.request) == row["request_hash"]
            and value.request == PortfolioCreate.model_validate_json(row["request"])
            and row["account_id"] == "bt-" + value.session["run_id"]
            and canonical(value).decode() == row["manifest"]
        )
        return value
