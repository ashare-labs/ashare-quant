"""JSON boundary for read-only analysis; no change to frozen result contracts."""

from typing import Literal

from pydantic import Field

from ashare_quant.contracts import Contract
from ashare_quant.tasks.contracts import JobID, JSONObject, Sha


class AnalysisMetric(Contract):
    value: float | None = Field(allow_inf_nan=False)
    unit: str
    status: Literal["ok", "unavailable"]
    reason: str | None
    sample_count: int = Field(ge=0)


class ResultAnalysis(Contract):
    schema_version: Literal["aq.analysis.v1"]
    calculation_version: Literal["returns-v1"]
    analysis_id: Sha
    job_id: JobID
    source_sha256: Sha
    engine: JSONObject
    policy: JSONObject
    initial_equity: float = Field(gt=0, allow_inf_nan=False)
    sample_count: int = Field(ge=1)
    metrics: dict[str, AnalysisMetric]
    series: list[JSONObject]
    benchmark: JSONObject | None
    drawdowns: list[JSONObject]
    monthly: list[JSONObject]
    rolling: list[JSONObject]
    costs: JSONObject
    trading: JSONObject
    quality: JSONObject
