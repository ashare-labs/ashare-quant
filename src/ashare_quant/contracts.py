"""Versioned wire contracts. Amounts are decimal strings, clocks are aware."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SHANGHAI = ZoneInfo("Asia/Shanghai")
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$", max_length=40)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}$")]
Security = Literal["600000.XSHG", "000001.XSHE"]
# The fixed replay protocol above remains intentionally finite. Public market
# reads use exchange-qualified identifiers independently of that fixture universe.
MarketSecurity = Annotated[str, Field(pattern=r"^[0-9]{6}\.(XSHG|XSHE|XBSE)$")]
Frequency = Literal["daily", "1m"]
Verdict = Literal["accepted", "rejected", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Guarantees(Contract):
    complete: bool = False
    fresh: bool = False
    final: bool = False
    tradable: bool = False
    turnover: bool = False
    corporate_actions: bool = False


class DataQuery(Contract):
    dataset_id: Identifier
    securities: tuple[Security, ...] = Field(min_length=1, max_length=2)
    frequency: Frequency
    as_of: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    visibility: Literal["assumed", "received", "verified"]
    guarantees: Guarantees = Field(default_factory=Guarantees)

    @model_validator(mode="after")
    def bounds(self):
        if self.start > self.end or self.end > self.as_of:
            raise ValueError("start <= end <= strategy as_of required")
        if len(set(self.securities)) != len(self.securities):
            raise ValueError("duplicate securities")
        return self


class DataAdmission(Contract):
    """A数达 attestation reference, never a recomputed quality verdict."""

    dataset_id: Identifier
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_kind: Literal["synthetic", "observed"]
    quality: Verdict
    quality_reference: str
    point_in_time_verified: bool
    requirements_met: bool
    limitations: tuple[str, ...] = ()


class DataBatch(Contract):
    query: DataQuery
    admission: DataAdmission
    source_payload_reference: str


class DataProvider(Protocol):
    def read(self, query: DataQuery) -> DataBatch:
        """Delegate admission to A数达; refuse unsupported guarantees without fallback."""
        ...


class CorporateActionFact(Contract):
    """Facts from A数达; engine alone decides and posts entitlement entries."""

    action_id: Identifier
    dataset_id: Identifier
    security: Security
    available_at: AwareDatetime
    record_date: date
    ex_date: date
    pay_date: date
    cash_per_share: Money
    source_reference: str


class StrategyCreate(Contract):
    engine_id: Literal["fixture-replay-v1", "rqalpha"] = "fixture-replay-v1"
    name: str = Field(min_length=1, max_length=80)
    scenario: Literal[
        "buy_hold", "t_plus_one", "insufficient_cash", "partial_fill",
        "cross_day", "corporate_action", "restart",
    ]
    description: str = Field(default="", max_length=2000)


class StrategyVersion(Contract):
    version_id: str
    definition: StrategyCreate
    parameters: dict[str, str | int]
    source_sha256: str
    created_at: AwareDatetime
    execution_status: Literal["specification_only", "trusted_builtin"] = "specification_only"


class RunCreate(Contract):
    strategy_version_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_id: Identifier = "paper-demo"
    engine_id: Literal["fixture-replay-v1", "rqalpha"] = "fixture-replay-v1"
    mode: Literal["fixture", "synthetic", "research", "paper"] = "fixture"
    idempotency_key: Identifier


class Position(Contract):
    security: Security
    quantity: int = Field(ge=0)
    sellable: int = Field(ge=0)
    average_price: Money = "0.00"
    dividend_receivable: Money = "0.00"

    @model_validator(mode="after")
    def quantities(self):
        if self.sellable > self.quantity:
            raise ValueError("sellable exceeds held quantity")
        return self


class Account(Contract):
    cash: Money
    frozen_cash: Money = "0.00"
    equity: Money
    positions: tuple[Position, ...] = ()

    @model_validator(mode="after")
    def snapshot_consistency(self):
        if Decimal(self.frozen_cash) > Decimal(self.cash):
            raise ValueError("frozen_cash exceeds cash")
        if len({p.security for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate position security")
        return self


class Order(Contract):
    order_id: Identifier
    security: Security
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    filled: int = Field(ge=0)
    limit_price: Money
    status: Literal["pending_new", "pending_cancel", "accepted", "partially_filled", "filled", "rejected", "cancelled"]
    reason: str | None = None

    @model_validator(mode="after")
    def quantities(self):
        if self.filled > self.quantity:
            raise ValueError("filled exceeds order quantity")
        return self


class Fill(Contract):
    fill_id: Identifier
    order_id: Identifier
    security: Security
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: Money
    fee: Money
    commission: Money = "0.00"
    tax: Money = "0.00"


class EngineStep(Contract):
    """One atomic projection/checkpoint boundary; no backend account arithmetic."""

    sequence: int = Field(ge=1)
    logical_at: AwareDatetime
    trading_day: date
    kind: Literal["open", "order", "fill", "close", "corporate_action"]
    message: str
    account: Account
    order: Order | None = None
    fill: Fill | None = None
    action_id: Identifier | None = None
    checkpoint: dict[str, str | int]

    @model_validator(mode="after")
    def market_date(self):
        if self.logical_at.astimezone(SHANGHAI).date() != self.trading_day:
            raise ValueError("trading_day must be the Asia/Shanghai logical date")
        return self


class EngineIdentity(Contract):
    engine_id: str
    version: str
    source_commit: str | None
    is_test_double: bool
    account_truth_owner: Literal["engine"] = "engine"


class EngineAdapter(Protocol):
    identity: EngineIdentity

    def next_step(self, checkpoint: dict[str, str | int] | None) -> EngineStep | None:
        """Resume engine-owned state; None means a complete stream, never fabricate fills."""
        ...


class RunView(Contract):
    run_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    account_id: str
    strategy_version_id: str
    cursor: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error: str | None
    cancel_requested_at: AwareDatetime | None = None
    reconciliation_required: bool = False
    native_started: bool = False
    native_reconcile_only: bool = False


class LogEntry(Contract):
    index: int
    observed_at: AwareDatetime
    level: Literal["INFO", "ERROR"]
    message: str


class EventPage(Contract):
    items: list[EngineStep]
    next_after: int
    has_more: bool


class ExecutionStatus(Contract):
    run_id: str
    status: str
    engine_id: str
    supervisor_lease_active: bool
    lease_expires_at: AwareDatetime | None
    last_error: str | None
    projection_cursor: int
    recovery: Literal["cli_worker_after_lease_expiry", "terminal", "worker_active", "cancel_reconciliation", "operator_reconciliation"]
    cancel_requested_at: AwareDatetime | None = None
    reconciliation_required: bool = False
    native_reconcile_only: bool = False


class ErrorBody(Contract):
    code: str
    message: str
    details: dict | None = None
    origin: str | None = None


class ErrorResponse(Contract):
    error: ErrorBody
    api_version: Literal["v1"] = "v1"


class ValidationResponse(Contract):
    detail: list[dict]


class StateView(Contract):
    run: RunView
    logical_at: AwareDatetime | None
    trading_day: date | None
    account: Account | None
    orders: list[Order]
    fills: list[Fill]
    truth_source: Literal["fixture-replay-v1", "rqalpha"] = "fixture-replay-v1"
    is_test_double: bool = True
    data_evidence_kind: Literal["synthetic"] = "synthetic"
    projection_complete: bool = False


class Capabilities(Contract):
    api_version: Literal["v1"] = "v1"
    backend_version: str
    storage: Literal["sqlite-control-and-projections"] = "sqlite-control-and-projections"
    executable_modes: tuple[Literal["fixture", "synthetic"], ...] = ("fixture", "synthetic")
    real_market_validation: Literal["not_run"] = "not_run"
    rqalpha_validation: Literal["optional_dependency_missing", "optional_native_installed_use_task_catalogue"]
    rqalpha_commit: str
    ashare_data_version: str
    ashare_data_commit: str | None
    ashare_data_tree: str | None
    ashare_data_integration: Literal["candidate_research_read_only"] = "candidate_research_read_only"
    ashare_data_admission: Literal["not_reviewed"] = "not_reviewed"
    limitations: tuple[str, ...]


class Manifest(Contract):
    schema_version: Literal[1] = 1
    backend_version: str
    backend_source_sha256: str
    request: RunCreate
    strategy: StrategyVersion
    engine: EngineIdentity
    data: DataAdmission
    fixture_sha256: str
    fixture_spec_sha256: str
    dependency_lock_sha256: str
    runtime_versions: dict[str, str] = Field(default_factory=dict)
    implementation_files: dict[str, str] = Field(default_factory=dict)
    native_runtime: dict = Field(default_factory=dict)
    candidate_data_version: str
    candidate_data_commit: str
    candidate_data_tree: str
    candidate_engine_commit: str
    initial_checkpoint: None = None
    risk_profile: Literal["fixture-only-no-real-orders", "rqalpha-synthetic-no-live-orders"] = "fixture-only-no-real-orders"
    execution_semantics: Literal["scripted_projection_replay", "rqalpha_native_synthetic"] = "scripted_projection_replay"
    real_strategy_executed: bool = False
    created_at: AwareDatetime
    trading_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, *, details=None, origin=None):
        self.code, self.message, self.status = code, message, status
        self.details, self.origin = details, origin
        super().__init__(message)

    def body(self):
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        if self.origin is not None:
            result["origin"] = self.origin
        return result
