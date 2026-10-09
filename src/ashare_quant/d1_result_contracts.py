"""Read-only fixed result v1: committed projections, separate author/reviewer status."""
from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from .contracts import Contract, Money
from .d1 import Sha256
from .d1_br1 import Assumption

SignedMoney = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]{1,8})?$", max_length=41)]
GitOID = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
EvidenceID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,95}$")]
ResultID = Literal["br1-600000-20200103-20200106-f9c58f0-v1"]


class ResultScope(Contract):
    grade: Literal["conditional_research"] = "conditional_research"
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["1d"] = "1d"
    initial_cash: Money
    order_quantity: Literal[100] = 100
    trading_dates: tuple[date, date]
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    event_clock_basis: Literal["declared_daily_model_schedule_not_market_receipt"] = "declared_daily_model_schedule_not_market_receipt"


class AuthorConclusion(Contract):
    status: Literal["PASS_FIXED_SCOPE"] = "PASS_FIXED_SCOPE"
    evidence_id: Literal["author.validation"] = "author.validation"
    independent_review: Literal[False] = False


class ResultReview(Contract):
    status: Literal["PASS_WITH_FIXED_SCOPE"] = "PASS_WITH_FIXED_SCOPE"
    reviewed_component: Literal["fixed_execution_candidate_only"] = "fixed_execution_candidate_only"
    reviewed_at: AwareDatetime
    scope: str
    summary_sha256: Sha256
    report_sha256: Sha256
    negative_and_consistency_checks: Literal[46] = 46
    wheel_tests: Literal[20] = 20
    execution_code_commit: GitOID
    evidence_commit: GitOID
    decision_evidence_id: Literal["independent.summary"] = "independent.summary"
    report_evidence_id: Literal["independent.report"] = "independent.report"


class ResultIndex(Contract):
    result_id: ResultID
    evidence_version: Literal["br1-f9c58f0-review-54f59787-v1"] = "br1-f9c58f0-review-54f59787-v1"
    evidence_manifest_sha256: Sha256
    result_snapshot_sha256: Sha256
    scope: ResultScope
    author_conclusion: AuthorConclusion
    independent_review: ResultReview


class ServingIdentity(Contract):
    role: Literal["read_only_result_service"] = "read_only_result_service"
    independent_review_status: Literal["not_reviewed"] = "not_reviewed"
    backend_version: str
    source_sha256: Sha256
    source_identity_basis: Literal["sha256_startup_pinned_backend_package_inventory"] = "sha256_startup_pinned_backend_package_inventory"
    contract_version: Literal["d1-br1-results.v1"] = "d1-br1-results.v1"


class ExecutionIdentity(Contract):
    backend_version: Literal["0.4.0.dev7"] = "0.4.0.dev7"
    code_commit: GitOID
    code_tree: GitOID
    wheel_sha256: Sha256
    release_sha256: Sha256
    native_run_identity: Sha256
    engine_name: Literal["RQAlpha"] = "RQAlpha"
    engine_version: Literal["6.4.1"] = "6.4.1"
    engine_commit: GitOID
    engine_wheel_sha256: Sha256


class ResultEvidence(Contract):
    evidence_id: EvidenceID
    type: Literal["run_freeze", "execution_release", "author_validation", "checkpoint_validation",
                  "scope_addendum", "committed_native_receipt", "completed_checkpoint", "runtime_manifest",
                  "process_result", "completion", "teardown", "attempt_trace_hash_only",
                  "committed_checkpoint_before_recovery", "assumptions_profile", "independent_review_summary",
                  "independent_review_report", "independent_review_checksums"]
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    verification: Literal["sha256_verified"] = "sha256_verified"


class SourceFact(Contract):
    key: str
    conclusion: Literal["supported", "unknown", "unsupported"]
    evidence_status: str
    limitations: tuple[str, ...]


class ResultDataIdentity(Contract):
    owner: Literal["ashare-data"] = "ashare-data"
    version: str
    commit: GitOID
    tree: GitOID
    wheel_sha256: Sha256
    dataset_id: Sha256
    manifest_sha256: Sha256
    price_dataset_id: Sha256
    facts_component_id: Sha256
    owner_profile_sha256: Sha256
    listing_contract_sha256: Sha256
    historical_pit_verified: Literal[False] = False
    source_absolute_listing_dates: Literal["unknown"] = "unknown"
    independent_data_admission: Literal["accepted_dev3_public_api_limited"] = "accepted_dev3_public_api_limited"
    source_facts_owner_version: str
    source_facts_dataset_id: Sha256
    source_facts_manifest_sha256: Sha256
    data_review_receipt_sha256: Sha256
    listing_review_receipt_sha256: Sha256
    source_facts: tuple[SourceFact, ...]


class ResultProfile(Contract):
    profile_id: Literal["D1-BR1-v1"] = "D1-BR1-v1"
    sha256: Sha256
    strict_plan_sha256: Sha256
    assumptions: tuple[Assumption, Assumption, Assumption]
    ad08_actual_calls: Literal[0] = 0
    ad08_execution_status: Literal["NOT_EXERCISED"] = "NOT_EXERCISED"
    limitations: tuple[str, ...]
    strict_execution: Literal["BLOCKED"] = "BLOCKED"
    paper_execution: Literal["BLOCKED"] = "BLOCKED"
    general_api_execution: Literal["BLOCKED"] = "BLOCKED"
    execution_permission: Literal[False] = False


class ResultOrder(Contract):
    order_id: str
    security: Literal["600000.XSHG"]
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    filled: int = Field(ge=0)
    status: Literal["filled"]
    submitted_at: AwareDatetime
    evidence_id: Literal["baseline.receipt"] = "baseline.receipt"


class ResultFill(Contract):
    fill_id: str
    order_id: str
    security: Literal["600000.XSHG"] = "600000.XSHG"
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: Money
    commission: Money
    tax: Money
    fee: Money
    filled_at: AwareDatetime
    evidence_id: Literal["baseline.receipt"] = "baseline.receipt"


class ResultAccount(Contract):
    cash: Money
    equity: Money
    quantity: int = Field(ge=0)
    sellable: int = Field(ge=0)
    average_price: Money
    dividend_receivable: Money
    native_daily_transaction_cost: Money
    evidence_id: Literal["baseline.receipt"] = "baseline.receipt"


class ResultAccountStage(Contract):
    sequence: int = Field(ge=1)
    phase: Literal["POST_BEFORE_TRADING", "POST_BAR", "POST_AFTER_TRADING", "POST_SETTLEMENT"]
    logical_at: AwareDatetime
    trading_date: date
    account: ResultAccount


class ResultSummary(Contract):
    initial_cash: Money
    final_cash: Money
    final_equity: Money
    net_change: SignedMoney
    net_change_basis: Literal["final_native_equity_minus_declared_initial_cash"] = "final_native_equity_minus_declared_initial_cash"
    commission_total: Money
    tax_total: Money
    fee_total: Money
    committed_order_count: int = Field(ge=0)
    committed_fill_count: int = Field(ge=0)
    final_committed_cursor: Literal[6] = 6


class ResultCheckpoint(Contract):
    evidence_id: EvidenceID
    committed_cursor: int = Field(ge=0, le=6)
    phase: Literal["BEFORE_TRADING", "BAR", "AFTER_TRADING", "SETTLEMENT"]
    generation: int = Field(ge=0)
    complete: bool
    committed_order_count: int = Field(ge=0)
    committed_fill_count: int = Field(ge=0)


class ResultRecovery(Contract):
    case_id: Literal["post-bar", "before-replace", "after-replace", "cross-day"]
    fault: Literal["post_bar:2", "before_replace:2", "after_replace:2", "after_event:4"]
    author_evidence_status: Literal["PASS"] = "PASS"
    independent_review_status: Literal["PASS_WITH_FIXED_SCOPE"] = "PASS_WITH_FIXED_SCOPE"
    independent_review_evidence_id: Literal["independent.summary"] = "independent.summary"
    before: ResultCheckpoint
    after: ResultCheckpoint
    final_equity: Money
    committed_projection_matches_baseline: Literal[True] = True
    committed_orders_unique: Literal[True] = True
    committed_fills_unique: Literal[True] = True
    fresh_process: Literal[True] = True
    replay_policy: Literal["uncommitted_events_may_replay_from_last_committed_cursor"] = "uncommitted_events_may_replay_from_last_committed_cursor"
    evidence_ids: tuple[EvidenceID, ...]


class ResultReentry(Contract):
    case_id: Literal["reenter-1", "reenter-2"]
    author_evidence_status: Literal["PASS"] = "PASS"
    independent_review_status: Literal["PASS_WITH_FIXED_SCOPE"] = "PASS_WITH_FIXED_SCOPE"
    independent_review_evidence_id: Literal["independent.summary"] = "independent.summary"
    committed_cursor: Literal[6] = 6
    receipt_reentry: Literal[True] = True
    fresh_process: Literal[True] = True
    checkpoint_unchanged: Literal[True] = True
    receipt_unchanged: Literal[True] = True
    attempt_trace_bytes_unchanged: Literal[True] = True
    additional_committed_orders: Literal[0] = 0
    additional_committed_fills: Literal[0] = 0
    evidence_ids: tuple[EvidenceID, ...]


class ResultWireRules(Contract):
    currency: Literal["CNY"] = "CNY"
    amounts: Literal["decimal_strings_up_to_8_places"] = "decimal_strings_up_to_8_places"
    native_projection: str = "Native RQAlpha float -> Decimal(str(value)) -> quantize(0.00000001), ROUND_HALF_EVEN; trailing zeros removed. This does not recover source decimal precision."
    time_rule: str = "Native naive model clocks are declared Asia/Shanghai and exposed with +08:00; they are not market receipt timestamps."
    stage_cost_rule: str = "native_daily_transaction_cost is the native day's value and can reset across days; do not sum it across account stages."
    net_change_rule: str = "Backend Decimal subtraction of final committed native equity and declared initial cash; the browser must not reconstruct a ledger from attempt traces."


class FrozenResult(Contract):
    result_id: ResultID
    evidence_version: Literal["br1-f9c58f0-review-54f59787-v1"]
    evidence_manifest_sha256: Sha256
    evidence_commit: GitOID
    evidence_tree: GitOID
    scope: ResultScope
    author_conclusion: AuthorConclusion
    independent_review: ResultReview
    execution_identity: ExecutionIdentity
    data_identity: ResultDataIdentity
    profile: ResultProfile
    summary: ResultSummary
    orders: tuple[ResultOrder, ...]
    fills: tuple[ResultFill, ...]
    account: ResultAccount
    account_stages: tuple[ResultAccountStage, ...]
    recoveries: tuple[ResultRecovery, ResultRecovery, ResultRecovery, ResultRecovery]
    reentries: tuple[ResultReentry, ResultReentry]
    wire_rules: ResultWireRules = Field(default_factory=ResultWireRules)
    evidence: tuple[ResultEvidence, ...]


class BR1ResultDetail(FrozenResult):
    api_version: Literal["v1"] = "v1"
    service_identity: ServingIdentity
    result_snapshot_sha256: Sha256


class BR1ResultList(Contract):
    api_version: Literal["v1"] = "v1"
    service_identity: ServingIdentity
    items: tuple[ResultIndex, ...]
