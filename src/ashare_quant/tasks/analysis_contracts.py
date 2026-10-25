"""JSON boundary for read-only analysis; no change to frozen result contracts."""

from typing import Annotated, Literal

from pydantic import Field

from ashare_quant.contracts import Contract
from ashare_quant.core.types import JSONObject, Sha
from ashare_quant.tasks.contracts import JobID

AnalysisVersion = Literal["returns-v1", "returns-v2"]
BenchmarkSelection = Annotated[str, Field(pattern=r"^(frozen|none|[0-9]{6}\.(XSHG|XSHE))$")]


class AnalysisMetric(Contract):
    value: float | None = Field(allow_inf_nan=False)
    unit: str
    status: Literal["ok", "unavailable"]
    reason: str | None
    sample_count: int = Field(ge=0)


class ResultAnalysis(Contract):
    schema_version: Literal["aq.analysis.v1"]
    calculation_version: AnalysisVersion
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
    comparison: JSONObject | None = None
    return_difference: AnalysisMetric | None = None
    excess_drawdowns: list[JSONObject] | None = None
