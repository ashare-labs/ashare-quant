"""Finite research task API. Trusted local callbacks and admitted end dates; no account reuse or broker mode."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from ashare_quant.backtests.daily.contracts import Sha, StrategyVersion
from ashare_quant.backtests.daily.execution_dates import EndDateSelection, ExecutionScope, ISODate
from ashare_quant.backtests.daily.parameter_policy import (
    BandParameters,
    JobParameters,
    ParameterLimits,
)
from ashare_quant.contracts import Contract
from ashare_quant.strategies.daily.rules import RuleStrategyCapabilities
from ashare_quant.strategies.python.decision import PythonDecisionReceipt
from ashare_quant.strategies.python.spec import PythonCapabilities
from ashare_quant.tasks.result_contracts import (
    GitOID,
    JSONObject,
    M2AccountProjection,
    M2Assumption,
    M2DayProjection,
    M2ResultScope,
    M2ResultSummary,
    WindowID,
)

JobID = Annotated[str, Field(pattern=r"^m2job-[0-9a-f]{32}$")]
JobStatus = Literal["queued", "running", "cancelling", "cancelled", "failed", "succeeded"]


class JobCreate(Contract):
    end_date: ISODate | None = None
    window_id: WindowID
    name: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^\S(?:[^\r\n]*\S)?$")]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")]
    assumption_ack_sha256: Sha
    data_binding_sha256: Sha
    parameters: JobParameters = BandParameters()


class JobServiceIdentity(Contract):
    backend_version: str
    source_sha256: Sha
    contract_version: Literal["m2-jobs.v6"] = "m2-jobs.v6"
    independent_review_status: Literal["not_reviewed"] = "not_reviewed"


class OwnerContractReview(Contract):
    status: Literal["not_reviewed"]
    receipt_sha256: Sha | None = None
    scope: Literal["data_production_contract_only"]
    execution_permission: Literal[False] = False


class PortableDataIdentity(Contract):
    owner_version: str
    owner_commit: GitOID | None
    owner_runtime_sha256: Sha
    dataset_id: Sha
    manifest_sha256: Sha
    profile_sha256: Sha
    query_contract_sha256: Sha
    data_binding_sha256: Sha
    source_evidence_kind: Literal["source_claim_unverified"] = "source_claim_unverified"
    original_quality: JSONObject
    owner_contract_review: OwnerContractReview
    original_owner_envelope_review: Literal["PENDING"] = "PENDING"
    original_owner_execution_authorized: Literal[False] = False
    external_owner_review_status: Literal["local_contract_verified_not_independently_reviewed"] = (
        "local_contract_verified_not_independently_reviewed"
    )
    owner_descriptor: JSONObject
    source_references: tuple[JSONObject, ...]


class AllowedWindow(Contract):
    scope: M2ResultScope
    assumption_ack_sha256: Sha
    assumptions: tuple[M2Assumption, ...]
    data_identity: PortableDataIdentity


class DataBindRequest(Contract):
    dataset_id: Sha


class JobCatalogue(Contract):
    end_date_selection: EndDateSelection = EndDateSelection()
    contract_version: Literal["m2-jobs.v6"] = "m2-jobs.v6"
    configured: bool
    configuration_error: str | None
    data_binding_sha256: Sha | None
    native_ready: bool
    native_error: str | None
    parameters: BandParameters = BandParameters()
    parameter_limits: ParameterLimits = ParameterLimits()
    rule_strategies: RuleStrategyCapabilities = RuleStrategyCapabilities()
    python_strategies: PythonCapabilities = PythonCapabilities()
    windows: tuple[AllowedWindow, ...]
    service_identity: JobServiceIdentity
    executor_version: str
    executor_source_sha256: Sha
    executor_review_scope: Literal["portable_revision_not_independently_reviewed"] = (
        "portable_revision_not_independently_reviewed"
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
    contract_version: Literal["m2-jobs.v6"] = "m2-jobs.v6"
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
    execution_scope: ExecutionScope
    schema_version: Literal["m2-job-manifest.v6"] = "m2-job-manifest.v6"
    job_id: JobID
    request: JobCreate
    service_identity: JobServiceIdentity
    executor_source_sha256: Sha
    runner_sha256: Sha
    parameters_sha256: Sha
    data_binding_sha256: Sha
    job_directory: JobID
    session_sha256: Sha
    session_file_sha256: Sha
    session: JSONObject
    strategy: StrategyVersion
    python_source_sha256: Sha | None = None
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


class PortableExecutionIdentity(Contract):
    backend_version: str
    source_sha256: Sha
    runtime_sha256: Sha
    identity_basis: Literal["installed_source_and_fixed_runtime"] = (
        "installed_source_and_fixed_runtime"
    )
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
    decision_verification: Literal["pure_rule_replay", "trusted_python_record.v1"] = "pure_rule_replay"
    python_decision: PythonDecisionReceipt | None = None
    native_reason: str | None
    native_rejections: tuple[NativeRejection, ...]


class JobResult(Contract):
    execution_scope: ExecutionScope
    strategy: StrategyVersion
    contract_version: Literal["m2-job-result.v6"] = "m2-job-result.v6"
    job_id: JobID
    request: JobCreate
    scope: M2ResultScope
    summary: M2ResultSummary
    account: M2AccountProjection
    days: tuple[ParameterDayProjection, ...]
    execution_identity: PortableExecutionIdentity
    service_identity: JobServiceIdentity
    data_identity: PortableDataIdentity
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
    executor_review_scope: Literal["portable_revision_not_independently_reviewed"] = (
        "portable_revision_not_independently_reviewed"
    )
    historical_pit_verified: Literal[False] = False
    paper_admitted: Literal[False] = False
    general_execution_permission: Literal[False] = False
    limitations: tuple[str, ...]
