"""Bounded Python workbench requests and frozen task identities."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.contracts import Contract
from ashare_quant.strategies.identity import StrategyID
from ashare_quant.tasks.contracts import JobID, JobView, JSONObject, Security, Sha


class PythonProject(Contract):
    project_id: str = Field(pattern=r"^aqproj-[0-9a-f]{32}$")
    entrypoint: str = Field(min_length=1)
    files: dict[str, str] = Field(min_length=1)


class PythonCreate(Contract):
    environment_id: Sha
    name: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    source: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    project: PythonProject | None = Field(default=None, exclude_if=lambda value: value is None)
    # Omit absent identities on serialization so old frozen manifests remain byte-identical.
    strategy_id: StrategyID | None = Field(default=None, exclude_if=lambda value: value is None)
    parameters: JSONObject = Field(default_factory=dict)
    frequency: Literal["1m", "1d"] = "1m"
    # Accepted for old clients; history is resolved at its actual API call.
    warmup_sessions: int = Field(default=20, ge=0, strict=True)
    securities: tuple[Security, ...] = ()
    start_date: date
    end_date: date
    initial_cash: float = Field(default=100000, strict=True, gt=0, le=10_000_000_000, allow_inf_nan=False)
    accept_research_model: Literal[True]
    trusted_local_code: Literal[True]

    @model_validator(mode="after")
    def bounds(self):
        if (self.source is None) == (self.project is None):
            raise ValueError("Supply exactly one of source or project")
        if self.project is not None and self.strategy_id != self.project.project_id:
            raise ValueError("strategy_id must match project.project_id")
        if self.project is None and self.strategy_id and self.strategy_id.startswith("aqproj-"):
            raise ValueError("Project identities require a complete project snapshot")
        if self.start_date > self.end_date or len(set(self.securities)) != len(self.securities):
            raise ValueError("Use ordered dates and unique securities")
        return self


class PythonScope(Contract):
    securities: tuple[Security, ...]
    trading_dates: tuple[date, ...]
    frequency: Literal["1m", "1d"] = "1m"


class PythonManifest(Contract):
    schema_version: Literal["python-minute-job.v1", "python-demand-job.v2", "python-project-job.v3"] = "python-demand-job.v2"
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


class TaskProgress(Contract):
    job_id: JobID
    attempt: int = Field(ge=1)
    sequence: int = Field(ge=1)
    sampled_at: AwareDatetime
    started_at: AwareDatetime
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    phase: Literal["preparing", "executing", "finalizing", "publishing"]
    completed_trading_days: int | None = Field(default=None, ge=0)
    total_trading_days: int | None = Field(default=None, ge=1)
    simulation_date: date | None = None
    current_rss_bytes: int | None = Field(default=None, ge=0)
    last_rss_bytes: int | None = Field(default=None, ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    memory_scope: Literal["strategy_process"] = "strategy_process"
    terminal: bool = False

    @model_validator(mode="after")
    def counts(self):
        if (self.total_trading_days is not None and self.completed_trading_days is not None
                and self.completed_trading_days > self.total_trading_days):
            raise ValueError("Completed days exceed the frozen execution scope")
        return self


class PythonJobView(JobView):
    contract_version: Literal["python-minute-jobs.v1"] = "python-minute-jobs.v1"
    request: PythonCreate
    phase: str = "queued"
    progress: TaskProgress | None = None


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
