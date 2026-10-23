"""Dispatch the shared queue to supported custom-code and factor executors."""

import json

from ashare_quant.contracts import DomainError
from ashare_quant.tasks.base import TaskService


class Jobs(TaskService):
    active_queue = True

    def for_job(self, row):
        try:
            schema = json.loads(row["manifest"])["schema_version"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("M2_JOB_IDENTITY_CONFLICT", "Stored task is malformed", 409) from exc
        if schema == "python-minute-job.v1":
            from ashare_quant.tasks.minute.service import PythonJobs

            return PythonJobs(self.repo.store, self.config)
        if schema == "factor-job.v1":
            from ashare_quant.tasks.factors.service import FactorJobs

            return FactorJobs(self.repo.store, self.config, sources={})
        raise DomainError(
            "TASK_FAMILY_RETIRED",
            "This task is retained as read-only history; submit custom Python for new research",
            409,
        )
