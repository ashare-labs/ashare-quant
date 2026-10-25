"""Typed factor selection and immutable result envelopes."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from ashare_quant.contracts import Contract
from ashare_quant.core.types import JSONObject, Sha
from ashare_quant.tasks.contracts import JobID, JobView


class EvaluationOptions(Contract):
    horizons: tuple[int, ...] = (1, 5, 10)
    quantiles: int = Field(default=5, ge=2, le=20)
    lag: int = Field(default=1, ge=1, le=252)
    direction: Literal[-1, 1] = 1
    min_assets: int = Field(default=5, strict=True, ge=2)
    cost_bps: tuple[float, ...] = (0, 5, 10, 20)
    rolling_window: int = Field(default=20, ge=2, le=2520)

    @model_validator(mode="after")
    def bounded(self):
        import math

        if not 1 <= len(self.horizons) <= 8 or len(set(self.horizons)) != len(self.horizons):
            raise ValueError("Use 1–8 unique horizons")
        if any(not 1 <= h <= 252 for h in self.horizons):
            raise ValueError("Horizons must be 1–252 trading sessions")
        if not 1 <= len(self.cost_bps) <= 8 or len(set(self.cost_bps)) != len(self.cost_bps):
            raise ValueError("Use 1–8 unique cost scenarios")
        if any(not math.isfinite(c) or not 0 <= c <= 1000 for c in self.cost_bps):
            raise ValueError("Costs must be finite one-way basis points in 0–1000")
        return self


class FactorCreate(Contract):
    environment_id: Sha
    idempotency_key: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    factor_id: str = Field(pattern=r"^(joinquant|alpha191|qlib_alpha158):[A-Za-z0-9_]+$")
    definition: str = Field(min_length=1, max_length=100)
    source_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    securities: tuple[str, ...] = Field(min_length=1)
    history_start: date
    start_date: date
    end_date: date
    label_end: date | None = None
    parameters: JSONObject = Field(default_factory=dict)
    rank_policy: Literal["strict", "available"] = "strict"
    evaluation: EvaluationOptions | None = None

    @model_validator(mode="after")
    def scope(self):
        import re

        if not self.history_start <= self.start_date <= self.end_date:
            raise ValueError("Use history_start <= start_date <= end_date")
        if self.label_end is not None and (
            self.evaluation is None or self.label_end < self.end_date
        ):
            raise ValueError("Label end requires evaluation and must be on or after signal end")
        if len(set(self.securities)) != len(self.securities) or any(
            not re.fullmatch(r"(?:[0-9]{6}\.X(?:SHG|SHE)|BJ\.[0-9]{6})", s) for s in self.securities
        ):
            raise ValueError("Use unique canonical securities")
        return self


class FactorScope(Contract):
    securities: tuple[str, ...]
    trading_dates: tuple[date, ...]


class FactorManifest(Contract):
    schema_version: Literal["factor-job.v1"] = "factor-job.v1"
    job_id: JobID
    request: FactorCreate
    source_sha256: Sha
    service_identity: JSONObject
    execution_scope: FactorScope
    session: JSONObject
    session_sha256: Sha
    session_file_sha256: Sha
    files: dict[str, Sha]


class FactorJobView(JobView):
    contract_version: Literal["factor-jobs.v1"] = "factor-jobs.v1"
    request: FactorCreate


class FactorJobList(Contract):
    items: tuple[FactorJobView, ...]


class FactorRow(Contract):
    date: date
    security: str
    value: float | None = Field(allow_inf_nan=False)
    value_text: str | None = None
    status: str
    reason: str | None = None


class FactorResult(Contract):
    schema_version: Literal["aq.factor-exploration.v1"] = "aq.factor-exploration.v1"
    job_id: JobID
    request: FactorCreate
    factor: JSONObject
    provenance: JSONObject
    rows: tuple[FactorRow, ...]
    summary: JSONObject
    evaluation: JSONObject | None = None
