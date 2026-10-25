"""Instance defaults; each accepted task freezes its execution budgets."""

import json
import os
import warnings
from typing import Annotated, Literal

from pydantic import Field

from ashare_quant.contracts import Contract

RETIRED_LIMITS = frozenset({
    "module_load_seconds", "callback_seconds", "execution_seconds", "preparation_seconds",
    "output_bytes", "file_bytes", "rss_bytes", "cpu_seconds", "open_files",
    "query_rows", "cached_rows", "read_rows",
})


class ServicePolicy(Contract):
    schema_version: Literal["m2-job-service-policy.v1"] = "m2-job-service-policy.v1"
    # Defaults are local operating budgets, not product capacity claims. The
    # integer ceiling is the signed native resource-counter representation.
    max_workers: Annotated[int, Field(strict=True, ge=1, le=2**63-1)] = 1
    max_pending: Annotated[int, Field(strict=True, ge=1, le=2**63-1)] = 32
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=2**63-1)] = 8
    admission_wait_seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)] = 2
    # Retained bytes per task, not a lifetime quota shared by historical results.
    artifact_bytes: Annotated[int, Field(strict=True, ge=1048576)] = 536870912
    # Decimal GB, deliberately separate from the per-task binary MiB budget.
    retained_artifact_bytes: Annotated[int, Field(strict=True, ge=1048576)] = 512000000000
    disk_free_bytes: Annotated[int, Field(strict=True, ge=1048576)] = 1073741824

    @classmethod
    def environment(cls):
        value = os.environ.get("ASHARE_QUANT_JOB_POLICY")
        if not value:
            return None
        parsed = json.loads(value)
        if isinstance(parsed, dict) and RETIRED_LIMITS.intersection(parsed):
            warnings.warn("Ignored retired execution limits: " +
                          ", ".join(sorted(RETIRED_LIMITS.intersection(parsed))),
                          UserWarning, stacklevel=2)
        return cls.from_frozen(parsed)

    @classmethod
    def from_frozen(cls, value):
        """Read old evidence without treating unused phase knobs as active limits.

        Its original bytes/hash remain authoritative and are never rewritten.
        These fields are absent from the current contract. Existing environment
        JSON also remains readable, with an explicit retirement warning.
        """
        if not isinstance(value, dict):
            return cls.model_validate(value)
        return cls.model_validate({k: v for k, v in value.items()
                                   if k not in RETIRED_LIMITS})


class ServiceStatus(Contract):
    contract_version: Literal["m2-job-service.v1"] = "m2-job-service.v1"
    job_contract_version: Literal["research-jobs.v1"] = "research-jobs.v1"
    policy: ServicePolicy
    policy_sha256: str
    initial_policy_sha256: str
    execution_policy_scope: Literal["frozen_per_task"] = "frozen_per_task"
    counts: dict[str, int]
    historical_counts: dict[str, int] = Field(default_factory=dict)
    unaccepted_task_directories: int = 0
    recovery_required: int
    available_pending: int
    queue_order: Literal["created_at_then_rowid"] = "created_at_then_rowid"
    resource_scope: Literal["per_database_local_trusted_processes"] = (
        "per_database_local_trusted_processes"
    )
    artifact_budget_scope: Literal["per_task"] = "per_task"
