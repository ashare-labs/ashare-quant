"""Small immutable M2 contracts; semantic bindings are checked by their consumers."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ashare_quant.contracts import SHANGHAI, Identifier
from ashare_quant.contracts import Contract as BaseContract
from ashare_quant.python_strategies.spec import PythonDefinition

from .common import decimal_text, digest
from .execution_dates import ISODate
from .owner_contracts import OwnerDocuments
from .quantity import TargetQuantity, valid_target
from .rules import RuleStrategyDefinition

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$", max_length=32)]
AssumptionID = Literal["A-EQ", "A-NORMAL", "A-AD08", "A-VIS", "A-CLOSE", "A-COST", "A-POOL"]
ASSUMPTIONS = ("A-EQ", "A-NORMAL", "A-AD08", "A-VIS", "A-CLOSE", "A-COST", "A-POOL")


class Contract(BaseContract):
    @field_validator("*", mode="before")
    @classmethod
    def exact_dates(cls, value, info):
        if cls.model_fields[info.field_name].annotation is date:
            if type(value) is date:
                return value
            if type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Exact ISO date required; no timestamp or datetime truncation")
        return value


class StrategyDefinition(BaseContract):
    template: Literal["buy_hold.v1", "previous_close_band.v1"]
    target_shares: TargetQuantity = Field(ge=100)
    enter_below: Money | None = None
    exit_above: Money | None = None

    @field_validator("enter_below", "exit_above")
    @classmethod
    def normalize(cls, value):
        return decimal_text(value) if value is not None else None

    @model_validator(mode="after")
    def parameters(self):
        if self.template == "buy_hold.v1":
            if self.enter_below is not None or self.exit_above is not None:
                raise ValueError("buy_hold has no price thresholds")
        elif self.enter_below is None or self.exit_above is None or not (
            Decimal(0) < Decimal(self.enter_below) < Decimal(self.exit_above)
        ):
            raise ValueError("require 0 < enter < exit")
        return self


StrategySpec = Annotated[StrategyDefinition | RuleStrategyDefinition | PythonDefinition, Field(discriminator="template")]


class M2StrategyVersion(Contract):
    version_id: Sha
    definition: StrategySpec
    source_sha256: Sha
    parameter_sha256: Sha
    created_at: AwareDatetime


# Keep the Python-local name concise without colliding with the frozen v1 schema.
StrategyVersion = M2StrategyVersion


class AssumptionAck(Contract):
    # Backend composite profile: includes owner profile, selected window and model policies.
    profile_sha256: Sha
    ids: tuple[AssumptionID, ...]

    @field_validator("ids")
    @classmethod
    def exact_set(cls, values):
        if len(values) != len(set(values)) or set(values) != set(ASSUMPTIONS):
            raise ValueError("Acknowledge all seven distinct scoped assumptions")
        return ASSUMPTIONS


class BacktestRequest(Contract):
    end_date: ISODate | None = None
    contract_version: Literal["m2.daily.v2"] = "m2.daily.v2"
    strategy_version_id: Sha
    data_binding_id: Sha
    window_id: Sha
    initial_cash: Money
    assumptions: AssumptionAck
    idempotency_key: Identifier
    mode: Literal["conditional_backtest"] = "conditional_backtest"
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["daily"] = "daily"
    visibility: Literal["assumed"] = "assumed"
    timing_profile: Literal["previous_day_0900_current_close.v1"] = "previous_day_0900_current_close.v1"
    initial_positions: Literal["empty"] = "empty"
    initial_entitlements: Literal["empty"] = "empty"

    @field_validator("initial_cash")
    @classmethod
    def cash_bounds(cls, value):
        if Decimal(value) <= 0:
            raise ValueError("Initial virtual cash must be positive")
        return decimal_text(value)


class DayScope(Contract):
    trading_date: date
    previous_open: date
    next_open: date
    owner_evidence_sha256: Sha

class WindowPlan(Contract):
    security: Literal["600000.XSHG"] = "600000.XSHG"
    days: tuple[DayScope, ...] = Field(min_length=1, max_length=20)
    owner_calendar_sha256: Sha
    read_domain_sha256: Sha  # Owner's price/listing/status/equity/warm-up/successor roles.

    @model_validator(mode="after")
    def exact_chain(self):
        for i, item in enumerate(self.days):
            if not item.previous_open < item.trading_date < item.next_open:
                raise ValueError("Strict owner predecessor/successor required")
            if i and (item.previous_open != self.days[i-1].trading_date or self.days[i-1].next_open != item.trading_date):
                raise ValueError("Window must be one contiguous owner-declared trading-day chain")
        if (self.days[-1].next_open - self.days[0].previous_open).days > 30:
            raise ValueError("Entire required read domain is at most 31 calendar days")
        return self

    @property
    def window_id(self):
        return digest(self)


class DataBinding(Contract):
    owner_version: str = Field(min_length=1, max_length=40)
    owner_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    owner_wheel_sha256: Sha | None
    owner_runtime_sha256: Sha | None = None
    dataset_id: Sha
    manifest_sha256: Sha
    use_profile_sha256: Sha
    query_contract_sha256: Sha
    independent_review_sha256: Sha | None
    backend_policy_sha256: Sha

    @property
    def binding_id(self):
        return digest(self)


class ModelProfile(Contract):
    binding_id: Sha
    window_id: Sha
    owner_use_profile_sha256: Sha
    visibility_policy_sha256: Sha
    matching_policy_sha256: Sha
    fee_policy_sha256: Sha
    pool_policy_sha256: Sha
    assumption_ids: tuple[AssumptionID, ...] = ASSUMPTIONS

    @model_validator(mode="after")
    def ordered_assumptions(self):
        if self.assumption_ids != ASSUMPTIONS:
            raise ValueError("Exact versioned assumption order required")
        return self

    @property
    def profile_sha256(self):
        return digest(self)


class AccountInput(Contract):
    run_id: Identifier
    account_id: Identifier
    source_kind: Literal["initial", "previous_day_committed"]
    commit_sequence: int = Field(strict=True, ge=0)
    snapshot_sha256: Sha
    account_as_of: AwareDatetime
    cash: Money
    quantity: int = Field(strict=True, ge=0)
    sellable: int = Field(strict=True, ge=0)

    @field_validator("cash")
    @classmethod
    def normalize_cash(cls, value):
        return decimal_text(value)

    @model_validator(mode="after")
    def position_bounds(self):
        if self.account_id != "bt-" + self.run_id or self.sellable > self.quantity:
            raise ValueError("One run-owned account with native bounded sellable quantity required")
        return self


class DecisionContext(Contract):
    run_id: Identifier
    request_sha256: Sha
    window_id: Sha
    trading_date: date
    previous_open: date
    logical_at: AwareDatetime
    phase: Literal["DECIDE"] = "DECIDE"
    event_cursor: int = Field(strict=True, ge=0)
    epoch: Identifier
    generation: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def clock(self):
        dt = self.logical_at.astimezone(SHANGHAI)
        if dt.date() != self.trading_date or (dt.hour, dt.minute, dt.second, dt.microsecond) != (9, 0, 0, 0):
            raise ValueError("Decision clock is exactly 09:00 Asia/Shanghai")
        if self.previous_open >= self.trading_date:
            raise ValueError("Closed prior day required")
        return self

    @property
    def authorization_sha256(self):
        return digest(self)


class AD08Tuple(Contract):
    security: Literal["600000.XSHG"] = "600000.XSHG"
    trading_date: date
    history_date: date
    adjust_origin: date
    frequency: Literal["1d"] = "1d"
    field: Literal["close"] = "close"
    count: int = Field(strict=True, ge=1, le=1, default=1)
    include_now: bool = Field(strict=True, default=False)
    skip_suspended: bool = Field(strict=True, default=False)
    adjustment_requested: Literal["pre"] = "pre"

    @model_validator(mode="after")
    def shape(self):
        if self.include_now or self.skip_suspended or self.adjust_origin != self.trading_date or self.history_date >= self.trading_date:
            raise ValueError("Unsupported AD08 tuple")
        return self


class OwnerStableEvidence(Contract):
    """Complete stable projection of our synthetic receipt, not an owner wire hash.

    binding_id is backend metadata added after a future codec checks owner evidence;
    it must never be included in the data owner's own evidence hash or envelope.
    """
    binding_id: Sha
    dataset_id: Sha
    use_profile_sha256: Sha
    query_contract_sha256: Sha
    window_id: Sha
    role: Literal["decision_previous_close"]
    call: AD08Tuple
    value: Money
    source_row_sha256: Sha
    evidence_kind: Literal["synthetic", "observed"]
    assumption_ids: tuple[AssumptionID, ...]

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value):
        return decimal_text(value)

    @field_validator("assumption_ids")
    @classmethod
    def canonical_dependencies(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Distinct assumption dependencies required")
        return tuple(a for a in ASSUMPTIONS if a in values)

    @model_validator(mode="after")
    def dependency(self):
        if len(self.assumption_ids) != len(set(self.assumption_ids)) or not {"A-EQ", "A-AD08"}.issubset(self.assumption_ids):
            raise ValueError("A-AD08 explicitly depends on A-EQ")
        if Decimal(self.value) <= 0:
            raise ValueError("Positive previous close required")
        return self


    @property
    def evidence_sha256(self):
        return digest({"schema": "m2.synthetic_owner_evidence.v1", "evidence": self})


class OwnerReadAuthorization(Contract):
    authorization_sha256: Sha
    generation: int = Field(strict=True, ge=0)


class OwnerReadReceipt(OwnerStableEvidence):
    """Flat synthetic transport with disjoint stable and authorization projections.

    Future codecs must retain and verify the published owner's complete Evidence
    and Authorization; this local identity must never replace the owner digest.
    """
    authorization_sha256: Sha
    generation: int = Field(strict=True, ge=0)

    @property
    def stable_evidence(self):
        return OwnerStableEvidence.model_validate({name: getattr(self, name) for name in OwnerStableEvidence.model_fields})

    @property
    def authorization(self):
        return OwnerReadAuthorization(authorization_sha256=self.authorization_sha256, generation=self.generation)

    @property
    def evidence_sha256(self):
        return self.stable_evidence.evidence_sha256


class NativeCallFact(Contract):
    origin_context: DecisionContext
    call: AD08Tuple
    owner_receipt: OwnerReadReceipt
    native_return_repr: str = Field(min_length=1, max_length=80)
    native_value: Money
    native_call_id: Identifier
    owner_documents: OwnerDocuments | None = None
    codec_observation: str | None = None


class ReadEvidence(Contract):
    call: AD08Tuple
    owner_receipt: OwnerReadReceipt
    native_return_repr: str = Field(min_length=1, max_length=80)
    native_value: Money
    native_call_id: Identifier
    origin_authorization_sha256: Sha
    consumption_authorization_sha256: Sha
    origin_generation: int = Field(strict=True, ge=0)
    consumption_generation: int = Field(strict=True, ge=0)
    cache_hit: bool = Field(strict=True)
    source_call_evidence_sha256: Sha | None = None
    origin_fact: NativeCallFact
    owner_documents: OwnerDocuments | None = None
    codec_observation: str | None = None
    verification_level: Literal["synthetic_contract_only", "owner_codec_only"] = "synthetic_contract_only"


class Signal(Contract):
    action: Literal["no_change", "target_shares"]
    target_shares: int | None = Field(default=None, strict=True)
    reason: Literal["hold_template", "enter_threshold", "exit_threshold", "inside_band", "rule_matched", "rules_no_match", "python_target", "python_no_change"]
    rule_index: int | None = Field(default=None, strict=True, ge=0, le=15)

    @model_validator(mode="after")
    def shape(self):
        if (self.reason == "rule_matched") != (self.rule_index is not None):
            raise ValueError("Only a matched rule has a rule index")
        if self.reason == "rule_matched" and self.action != "target_shares":
            raise ValueError("Matched rules produce a target")
        if self.reason == "rules_no_match" and self.action != "no_change":
            raise ValueError("Unmatched rules preserve the current target")
        if self.reason == "python_target" and self.action != "target_shares":
            raise ValueError("Python target requires a target signal")
        if self.reason == "python_no_change" and self.action != "no_change":
            raise ValueError("Python no-change requires no_change")
        if self.action == "no_change" and self.target_shares is not None:
            raise ValueError("no_change is not an executed no_order")
        if self.action == "target_shares" and not valid_target(self.target_shares):
            raise ValueError("Target must be a nonnegative, exactly representable whole lot")
        return self
