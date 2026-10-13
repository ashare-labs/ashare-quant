"""Single-host service limits, persisted independently of the stable v6 job contract."""

import json
import os
from typing import Annotated, Literal

from pydantic import Field

from ashare_quant.contracts import Contract


class ServicePolicy(Contract):
    schema_version: Literal["m2-job-service-policy.v1"] = "m2-job-service-policy.v1"
    max_workers: Annotated[int, Field(strict=True, ge=1, le=4)] = 1
    max_pending: Annotated[int, Field(strict=True, ge=1, le=128)] = 32
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=32)] = 8
    admission_wait_seconds: Annotated[float, Field(gt=0, le=10)] = 2
    execution_seconds: Annotated[int, Field(strict=True, ge=1, le=120)] = 120
    preparation_seconds: Annotated[int, Field(strict=True, ge=1, le=60)] = 60
    output_bytes: Annotated[int, Field(strict=True, ge=1024, le=1048576)] = 1048576
    file_bytes: Annotated[int, Field(strict=True, ge=1048576, le=67108864)] = 67108864
    rss_bytes: Annotated[int, Field(strict=True, ge=16777216, le=2147483648)] = 1073741824
    cpu_seconds: Annotated[int, Field(strict=True, ge=1, le=120)] = 120
    open_files: Annotated[int, Field(strict=True, ge=64, le=512)] = 256

    @classmethod
    def environment(cls):
        value = os.environ.get("ASHARE_QUANT_JOB_POLICY")
        return cls.model_validate(json.loads(value)) if value else None


class ServiceStatus(Contract):
    contract_version: Literal["m2-job-service.v1"] = "m2-job-service.v1"
    job_contract_version: Literal["m2-jobs.v6"] = "m2-jobs.v6"
    policy: ServicePolicy
    policy_sha256: str
    counts: dict[str, int]
    unaccepted_task_directories: int = 0
    recovery_required: int
    available_pending: int
    queue_order: Literal["created_at_then_rowid"] = "created_at_then_rowid"
    resource_scope: Literal["per_database_local_trusted_processes"] = (
        "per_database_local_trusted_processes"
    )
