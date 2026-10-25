"""General queue and wire types, independent of strategy templates and windows."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ashare_quant.contracts import Contract
from ashare_quant.core.types import ISODate as ISODate
from ashare_quant.core.types import JSONObject as JSONObject
from ashare_quant.core.types import ResearchSecurity
from ashare_quant.core.types import Sha as Sha
from ashare_quant.core.types import exact_date as exact_date

Security = ResearchSecurity

JobID = Annotated[str, Field(pattern=r"^m2job-[0-9a-f]{32}$")]
ACTIVE_SCHEMAS = ("python-minute-job.v1", "python-demand-job.v2", "python-project-job.v3", "factor-job.v1")
JobStatus = Literal["queued", "running", "cancelling", "cancelled", "failed", "succeeded"]

class ResultReference(Contract):
    result_id: JobID
    href: str
    snapshot_sha256: Sha
    evidence_manifest_sha256: Sha
    publication: Annotated[str, Field(pattern=r"^publication-[1-9][0-9]*$")]



class JobView(Contract):
    contract_version: Literal["research-jobs.v1"] = "research-jobs.v1"
    job_id: JobID
    request: JSONObject
    status: JobStatus
    stored_status: JobStatus | None = None
    display_status: str | None = None
    execution_state: str = "current_runtime"
    execution_reason: JSONObject | None = None
    account_id: str
    manifest_sha256: Sha
    session_sha256: Sha
    created_at: AwareDatetime
    updated_at: AwareDatetime
    attempt_count: int
    committed_cursor: int
    total_cursors: int
    checkpoint_complete: bool
    cancellation_requested_at: AwareDatetime | None
    recovery_required: bool
    can_resume: bool
    result: ResultReference | None
    error: JSONObject | None



class JobLog(Contract):
    sequence: int
    at: AwareDatetime
    event: str
    details: JSONObject



class JobLogs(Contract):
    job_id: JobID
    items: tuple[JobLog, ...]






def execution_presentation(status, manifest, current_source):
    """Explain historical execution eligibility without changing stored task state."""
    if manifest.get("schema_version") not in ACTIVE_SCHEMAS:
        state = "retired_family"
        reason = {"code": "TASK_FAMILY_RETIRED", "message": "此任务家族已退役，保留只读历史；新研究请提交自定义 Python。"}
    elif manifest.get("service_identity", {}).get("source_sha256") != current_source:
        state = "runtime_unavailable"
        reason = {"code": "M2_JOB_SERVICE_CHANGED", "message": "任务绑定旧运行时，当前 worker 不会执行；可读取历史，或明确创建新研究。"}
    else:
        state, reason = "current_runtime", None
    display = status
    if status in {"queued", "running", "cancelling"} and reason:
        display = "retired" if state == "retired_family" else "runtime_unavailable"
    return dict(stored_status=status, display_status=display, execution_state=state, execution_reason=reason)
