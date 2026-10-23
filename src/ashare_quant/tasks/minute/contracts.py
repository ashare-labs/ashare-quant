"""Bounded Python workbench requests and frozen task identities."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from ashare_quant.contracts import Contract
from ashare_quant.tasks.contracts import JobID, JobView, JSONObject, Security, Sha


class PythonCreate(Contract):
    environment_id: Sha
    name: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1, max_length=262144)
    parameters: JSONObject = Field(default_factory=dict)
    frequency: Literal["1m", "1d"] = "1m"
    warmup_sessions: int = Field(default=20, ge=1, le=120, strict=True)
    securities: tuple[Security, ...] = Field(min_length=1, max_length=8)
    start_date: date
    end_date: date
    initial_cash: float = Field(default=100000, gt=0, le=100000000, allow_inf_nan=False)
    accept_research_model: Literal[True]
    trusted_local_code: Literal[True]

    @model_validator(mode="after")
    def bounds(self):
        if self.start_date > self.end_date or len(set(self.securities)) != len(self.securities):
            raise ValueError("Use ordered dates and unique securities")
        limit = 186 if self.frequency == "1d" else 31
        if (self.end_date - self.start_date).days > limit:
            raise ValueError(f"Use at most {limit} calendar days per local task")
        return self


class PythonScope(Contract):
    securities: tuple[Security, ...]
    trading_dates: tuple[date, ...]
    frequency: Literal["1m", "1d"] = "1m"


class PythonManifest(Contract):
    schema_version: Literal["python-minute-job.v1"] = "python-minute-job.v1"
    job_id: JobID
    request: PythonCreate
    source_sha256: Sha
    service_identity: JSONObject
    execution_scope: PythonScope
    release: JSONObject
    session: JSONObject
    session_sha256: Sha
    session_file_sha256: Sha
    files: dict[str, Sha]


class PythonJobView(JobView):
    contract_version: Literal["python-minute-jobs.v1"] = "python-minute-jobs.v1"
    request: PythonCreate
    phase: str = "queued"


class PythonJobList(Contract):
    items: tuple[PythonJobView, ...]


class PythonResult(Contract):
    job_id: JobID
    name: str
    release: JSONObject
    parameters: JSONObject
    preparation: JSONObject
    native: JSONObject
    summary: JSONObject
    logs: tuple[JSONObject, ...]
