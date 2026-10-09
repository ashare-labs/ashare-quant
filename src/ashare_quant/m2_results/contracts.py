"""Versioned read-only M2 projections. All market money stays decimal text."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from ashare_quant.contracts import Contract, Money
from ashare_quant.d1_result_contracts import GitOID, SignedMoney
from ashare_quant.m2.contracts import AccountInput, NativeCallFact, Sha, Signal, StrategyVersion
from ashare_quant.m2.journal import NativeOrderProjection

WindowID = Literal["m2a", "w1", "w2"]
JSONObject = dict[str, JsonValue]


class M2ServingIdentity(Contract):
    role: Literal["read_only_result_service"] = "read_only_result_service"
    contract_version: Literal["m2-results.v1"] = "m2-results.v1"
    backend_version: str
    source_sha256: Sha
    source_identity_basis: Literal["sha256_startup_pinned_backend_package_inventory"] = (
        "sha256_startup_pinned_backend_package_inventory"
    )
    independent_review_status: Literal["not_reviewed"] = "not_reviewed"


class M2ResultReview(Contract):
    status: Literal["PASS_BOUNDED_NATIVE_RESEARCH"]
    reviewed_component: Literal["fixed_dev11_execution_only"] = "fixed_dev11_execution_only"
    reviewed_at: AwareDatetime
    execution_commit: GitOID
    summary_sha256: Sha
    report_sha256: Sha
    evidence_ids: tuple[str, ...] = ("review.summary", "review.report", "review.checksums")


class M2ResultScope(Contract):
    window_id: WindowID
    grade: Literal["conditional_research"] = "conditional_research"
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["1d"] = "1d"
    trading_dates: tuple[date, ...]
    initial_cash: Money
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    clock_basis: Literal["declared_daily_model_schedule_not_market_receipt"] = (
        "declared_daily_model_schedule_not_market_receipt"
    )


class M2AccountProjection(Contract):
    cash: Money
    equity: Money
    quantity: int = Field(ge=0, strict=True)
    sellable: int = Field(ge=0, strict=True)
    average_price: Money
    dividend_receivable: Money
    native_daily_transaction_cost: Money


class M2ResultSummary(Contract):
    initial_cash: Money
    final_cash: Money
    final_equity: Money
    net_change: SignedMoney
    final_quantity: int = Field(ge=0)
    commission_total: Money
    tax_total: Money
    fee_total: Money
    committed_order_count: int = Field(ge=0)
    committed_fill_count: int = Field(ge=0)
    actual_history_call_count: int = Field(ge=0)
    native_cache_consumption_count: int = Field(ge=0)


class M2ResultIndex(Contract):
    result_id: Annotated[
        str, Field(pattern=r"^m2-(m2a|w1|w2)-600000-[0-9]{8}-[0-9]{8}-fa91a75-v1$")
    ]
    evidence_version: Literal["m2-fa91a75-review-8c55cbe4-v1"]
    evidence_manifest_sha256: Sha
    scope: M2ResultScope
    strategy: StrategyVersion
    summary: M2ResultSummary
    independent_review: M2ResultReview


class M2FillProjection(Contract):
    fill_id: str
    order_id: str
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, strict=True)
    price: Money
    commission: Money
    tax: Money
    fee: Money
    filled_at: AwareDatetime
    evidence_id: str


class M2AD08Projection(Contract):
    native_call_id: str
    native_history_caller: Literal["_get_prev_close"]
    native_array_dtype: Literal["float64"]
    native_array_shape: tuple[Literal[1]]
    history_call: JSONObject
    argument_types: dict[str, str]
    previous_close: Money
    cache_hit: Literal[True]
    actual_native_observed: Literal[True] = True
    codec_native_call_observed: Literal[False] = False
    consumption_authorization_sha256: Sha
    owner_document_sha256: dict[str, Sha]
    owner_documents: dict[str, JSONObject]
    first_call: NativeCallFact
    observed_returns: tuple[JSONObject, ...]
    evidence_id: str


class M2DayProjection(Contract):
    trading_date: date
    previous_open: date
    decision_at: AwareDatetime
    decision_id: Sha
    economic_input_sha256: Sha
    signal: Signal
    prior_committed_account: AccountInput
    ad08: M2AD08Projection
    execution_at: AwareDatetime
    execution_close: Money | None
    execution_price_read_status: Literal["owner_execution_bar_observed", "not_read_no_order"]
    execution_commit_sequence: int
    decision_commit_sequence: int
    day_close_commit_sequence: int
    decision_cursor: int
    execution_cursor: int
    day_close_cursor: int
    no_order_reason: str | None
    orders: tuple[NativeOrderProjection, ...]
    fills: tuple[M2FillProjection, ...]
    account_at_execution: M2AccountProjection
    account_at_day_close: M2AccountProjection
    day_close_at: AwareDatetime
    day_fee: Money
    cumulative_fee: Money
    native_receipt_sha256: Sha
    evidence_id: str


class M2ExecutionIdentity(Contract):
    backend_version: Literal["0.4.0.dev11"] = "0.4.0.dev11"
    code_commit: GitOID
    code_tree: GitOID
    source_sha256: Sha
    wheel_sha256: Sha
    native_run_identity: Sha
    session_sha256: Sha
    engine: Literal["RQAlpha"] = "RQAlpha"
    engine_version: Literal["6.4.1"] = "6.4.1"
    engine_commit: Literal["0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"] = (
        "0d98adefa87956e26f3e7ca5b26b5f3fc7ca834f"
    )


class M2DataIdentity(Contract):
    owner_version: str
    owner_commit: GitOID
    owner_wheel_sha256: Sha
    dataset_id: Sha
    manifest_sha256: Sha
    profile_sha256: Sha
    query_contract_sha256: Sha
    data_review_sha256: Sha
    source_evidence_kind: Literal["source_claim_unverified"] = "source_claim_unverified"
    original_quality: JSONObject
    original_owner_envelope_review: Literal["PENDING"] = "PENDING"
    original_owner_execution_authorized: Literal[False] = False
    external_owner_review_status: Literal["PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"] = (
        "PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"
    )
    owner_descriptor: JSONObject
    source_references: tuple[JSONObject, ...]


class M2Assumption(Contract):
    assumption_id: str
    owner: Literal["ashare-data", "ashare-quant"]
    description: str
    sha256: Sha
    version: str


class M2RecoveryCase(Contract):
    fault: str
    tested_window: Literal["w1"] = "w1"
    source_status: Literal["PASS"] = "PASS"
    wheel_status: Literal["PASS", "NOT_REPEATED"]
    native_states_byte_equal: tuple[str, ...]
    native_extras_equal: Literal[True]
    economic_equal: Literal[True]
    crash_snapshots: JSONObject
    evidence_ids: tuple[str, ...]


class M2RecoverySummary(Contract):
    selected_window_recovery_exercised: bool
    selected_window_completed_reentries: Literal[2] = 2
    independent_source_recovery_count: Literal[9] = 9
    independent_wheel_recovery_count: Literal[7] = 7
    cases: tuple[M2RecoveryCase, ...]
    evidence_ids: tuple[str, ...]


class M2EvidenceEntry(Contract):
    evidence_id: str
    type: str
    sha256: Sha
    size_bytes: int = Field(ge=0)
    verification: Literal["sha256_verified"] = "sha256_verified"
    provenance: JSONObject


class M2FrozenResult(M2ResultIndex):
    execution_identity: M2ExecutionIdentity
    data_identity: M2DataIdentity
    assumptions: tuple[M2Assumption, ...]
    assumption_bundle_sha256: Sha
    account: M2AccountProjection
    days: tuple[M2DayProjection, ...]
    recovery: M2RecoverySummary
    evidence: tuple[M2EvidenceEntry, ...]
    limitations: tuple[str, ...]
    general_execution_permission: Literal[False] = False
    paper_admitted: Literal[False] = False
    historical_pit_verified: Literal[False] = False
    valuation_bar_query_observed: Literal[False] = False
    unique_baseline_dates_across_catalogue: Literal[11] = 11


class M2ResultDetail(M2FrozenResult):
    service_identity: M2ServingIdentity
    result_snapshot_sha256: Sha


class M2ResultList(Contract):
    contract_version: Literal["m2-results.v1"] = "m2-results.v1"
    service_identity: M2ServingIdentity
    evidence_manifest_sha256: Sha
    items: tuple[M2ResultIndex, ...]
