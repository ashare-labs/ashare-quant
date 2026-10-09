"""Finite research task API. No user code, free dates, account reuse or broker mode."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ashare_quant.contracts import Contract
from ashare_quant.m2.contracts import Sha
from ashare_quant.m2.parameter_policy import BandParameters, ParameterLimits
from ashare_quant.m2_results.contracts import (
    GitOID,
    JSONObject,
    M2AccountProjection,
    M2Assumption,
    M2DataIdentity,
    M2DayProjection,
    M2ResultScope,
    M2ResultSummary,
    WindowID,
)

JobID = Annotated[str, Field(pattern=r"^m2job-[0-9a-f]{32}$")]
JobStatus = Literal["queued", "running", "cancelling", "cancelled", "failed", "succeeded"]


class JobCreate(Contract):
    window_id: WindowID
    name: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^\S(?:[^\r\n]*\S)?$")]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")]
    assumption_ack_sha256: Sha
    parameters: BandParameters = BandParameters()


class JobServiceIdentity(Contract):
    backend_version: str
    source_sha256: Sha
    contract_version: Literal["m2-jobs.v2"] = "m2-jobs.v2"
    independent_review_status: Literal["not_reviewed"] = "not_reviewed"


class AllowedWindow(Contract):
    scope: M2ResultScope
    reviewed_example_strategy_version_id: Sha
    assumption_ack_sha256: Sha
    assumptions: tuple[M2Assumption, ...]
    data_identity: M2DataIdentity
    reviewed_example_id: str


class JobCatalogue(Contract):
    contract_version: Literal["m2-jobs.v2"] = "m2-jobs.v2"
    configured: bool
    configuration_error: str | None
    parameters: BandParameters = BandParameters()
    parameter_limits: ParameterLimits = ParameterLimits()
    windows: tuple[AllowedWindow, ...]
    service_identity: JobServiceIdentity
    executor_version: Literal["0.4.0.dev15+exec1"] = "0.4.0.dev15+exec1"
    executor_wheel_sha256: Sha
    executor_review_scope: Literal["parameter_revision_and_new_jobs_not_independently_reviewed"] = (
        "parameter_revision_and_new_jobs_not_independently_reviewed"
    )
    general_backtests_enabled: Literal[False] = False
    paper_live_enabled: Literal[False] = False
    historical_pit_verified: Literal[False] = False


class ResultReference(Contract):
    result_id: JobID
    href: str
    snapshot_sha256: Sha
    evidence_manifest_sha256: Sha
    publication: Annotated[str, Field(pattern=r"^publication-[1-9][0-9]*$")]


class JobView(Contract):
    contract_version: Literal["m2-jobs.v2"] = "m2-jobs.v2"
    job_id: JobID
    request: JobCreate
    status: JobStatus
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


class JobList(Contract):
    items: tuple[JobView, ...]


class JobLog(Contract):
    sequence: int
    at: AwareDatetime
    event: str
    details: JSONObject


class JobLogs(Contract):
    job_id: JobID
    items: tuple[JobLog, ...]


class JobManifest(Contract):
    schema_version: Literal["m2-job-manifest.v2"] = "m2-job-manifest.v2"
    job_id: JobID
    request: JobCreate
    service_identity: JobServiceIdentity
    executor_wheel_sha256: Sha
    executor_source_sha256: Sha
    runner_sha256: Sha
    parameters_sha256: Sha
    owner_store: str
    job_directory: str
    session_sha256: Sha
    session_file_sha256: Sha
    session: JSONObject
    approved_window: AllowedWindow
    prepared_proof: JSONObject


class HistoricalEnginePriceRead(Contract):
    operation: Literal["execution_bar", "valuation_bar"]
    trading_date: date
    context: JSONObject
    owner_documents: JSONObject
    owner_document_sha256: dict[str, Sha]
    native_observation_index: int
    verification: Literal["committed_historical_integrity_only"] = (
        "committed_historical_integrity_only"
    )
    authorizes_new_read: Literal[False] = False


class ParameterExecutionIdentity(Contract):
    backend_version: Literal["0.4.0.dev15+exec1"] = "0.4.0.dev15+exec1"
    base_backend_version: Literal["0.4.0.dev11"] = "0.4.0.dev11"
    base_code_commit: GitOID
    base_code_tree: GitOID
    source_sha256: Sha
    wheel_sha256: Sha
    release_sha256: Sha
    native_run_identity: Sha
    session_sha256: Sha
    engine: Literal["RQAlpha"] = "RQAlpha"
    engine_version: Literal["6.4.1"] = "6.4.1"
    engine_commit: Literal["0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"] = (
        "0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"
    )


class NativeRejection(Contract):
    observed_at: AwareDatetime
    event: Literal["ORDER_CREATION_REJECT"] = "ORDER_CREATION_REJECT"
    phase: Literal["SUBMIT_MATCH"] = "SUBMIT_MATCH"
    native_observation_index: int
    generation: int
    cursor: int
    order_id: str | None
    reason: str


class ParameterDayProjection(M2DayProjection):
    native_reason: str | None
    native_rejections: tuple[NativeRejection, ...]


class JobResult(Contract):
    contract_version: Literal["m2-job-result.v2"] = "m2-job-result.v2"
    job_id: JobID
    request: JobCreate
    scope: M2ResultScope
    summary: M2ResultSummary
    account: M2AccountProjection
    days: tuple[ParameterDayProjection, ...]
    execution_identity: ParameterExecutionIdentity
    service_identity: JobServiceIdentity
    data_identity: M2DataIdentity
    assumptions: tuple[M2Assumption, ...]
    assumption_bundle_sha256: Sha
    recovery: JSONObject
    evidence: JSONObject
    manifest_sha256: Sha
    valuation_bar_query_observed: bool
    engine_price_reads: tuple[HistoricalEnginePriceRead, ...]
    result_status: Literal["completed_not_independently_reviewed"] = (
        "completed_not_independently_reviewed"
    )
    executor_review_scope: Literal["parameter_revision_not_independently_reviewed"] = (
        "parameter_revision_not_independently_reviewed"
    )
    historical_pit_verified: Literal[False] = False
    paper_admitted: Literal[False] = False
    general_execution_permission: Literal[False] = False
    limitations: tuple[str, ...]
