"""Frozen inputs, not another strategy catalogue or an AD execution attestation."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.contracts import Contract, Identifier, Money

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
StrategyId = Literal["equal-weight", "momentum", "trend"]
Security = Annotated[str, Field(pattern=r"^[0-9]{6}\.(XSHG|XSHE)$")]
Quantity = Annotated[int, Field(strict=True, ge=0, le=10**9)]


class RiskPolicy(Contract):
    profile: Literal["conservative_v1", "target_then_native_cash_v1"] = "conservative_v1"
    max_gross: Money = "0.80"
    max_single: Money = "0.40"
    cash_buffer: Money = "0.10"
    max_turnover: Money = "1.00"
    max_order_value: Money = "50000"
    max_holdings: int = Field(strict=True, ge=1, le=10, default=8)
    allow_st: Literal[False] = False
    max_order_nav_ratio: Money = "0.40"
    execution_max_single: Money = "0.40"

    @model_validator(mode="after")
    def ratios(self):
        for key in (
            "max_gross",
            "max_single",
            "cash_buffer",
            "max_order_nav_ratio",
            "execution_max_single",
        ):
            if not 0 <= Decimal(getattr(self, key)) <= 1:
                raise ValueError("Risk ratios must be in [0,1]")
        if (
            not 0
            <= Decimal(self.max_turnover)
            <= (2 if self.profile == "target_then_native_cash_v1" else 1)
        ):
            raise ValueError("Two-sided turnover is bounded by 2 for unlevered target simulation")
        if Decimal(self.max_order_value) <= 0:
            raise ValueError("Positive order ceiling required")
        return self


class SessionConfig(Contract):
    schema_version: Literal["paper-session.v1"] = "paper-session.v1"
    account_id: Identifier
    strategy_version: Sha
    scenario_manifest_sha256: Sha
    initial_cash: Money
    universe: tuple[Security, ...] = Field(min_length=1, max_length=10)
    risk: RiskPolicy = RiskPolicy()
    commission_rate: Money = "0.0003"
    minimum_commission: Money = "5"
    sell_tax_rate: Money = "0.001"
    model: Literal["prior-close-frozen-targets-daily-open.v1"] = (
        "prior-close-frozen-targets-daily-open.v1"
    )
    data_mode: Literal["synthetic_fixture", "historical_research"] = "synthetic_fixture"
    strategy_id: StrategyId | None = None
    replay_binding_sha256: Sha | None = None
    cash_dividend_policy: Literal["reject", "gross_zero_withholding_0900"] = "reject"
    execution_admission_scope: Literal["whole_window", "per_order"] = "whole_window"

    @model_validator(mode="after")
    def scope(self):
        if (self.strategy_id is None) != (self.replay_binding_sha256 is None):
            raise ValueError("Strategy and local replay binding must be supplied together")
        if self.data_mode == "historical_research" and self.strategy_id is None:
            raise ValueError("Historical replay requires a pinned local owner binding")
        if self.execution_admission_scope == "per_order" and (
            self.data_mode != "historical_research" or self.strategy_id is None
        ):
            raise ValueError("Per-order AD admission requires a pinned historical strategy binding")
        if len(set(self.universe)) != len(self.universe) or Decimal(self.initial_cash) <= 0:
            raise ValueError("Unique securities and positive initial cash required")
        for key in ("commission_rate", "sell_tax_rate"):
            if not 0 <= Decimal(getattr(self, key)) <= 1:
                raise ValueError("Invalid rate")
        return self


class DecisionRow(Contract):
    security: Security
    price_date: date
    previous_close: Money
    observed_at: AwareDatetime
    complete: bool = Field(strict=True)
    source_watermark: date
    selected_watermark: date
    eligible: bool | None = Field(strict=True)
    suspended: bool | None = Field(strict=True)
    is_st: bool | None = Field(strict=True)
    lower_limit: Money | None
    upper_limit: Money | None
    rule_version: str | None
    actions_known_absent: bool = Field(strict=True)
    action_effect: Literal["none", "zero_entitlement", "native_cash_dividend"] = "none"
    facts_available_at: AwareDatetime
    round_lot: Literal[100] = 100


class CashDividend(Contract):
    security: Security
    record_date: date
    ex_date: date
    pay_date: date
    cash_per_share: Money
    record_quantity: Quantity
    record_checkpoint: Sha
    source_event: dict
    model_cash_available_at: AwareDatetime


class Decision(Contract):
    schema_version: Literal["paper-decision.v1"] = "paper-decision.v1"
    trading_date: date
    previous_open: date
    next_open: date
    logical_at: AwareDatetime
    received_at: AwareDatetime
    calendar_available_at: AwareDatetime
    decision_deadline: AwareDatetime
    expected_parent: Sha | None
    strategy_version: Sha
    strategy_receipt_sha256: Sha
    data_manifest_sha256: Sha
    evidence_kind: Literal["synthetic", "historical_research"] = "synthetic"
    clock_basis: Literal["synthetic_clock", "retrospective_model"] = "synthetic_clock"
    strategy_receipt: dict | None = None
    cash_dividends: tuple[CashDividend, ...] = ()
    order_admissions: tuple[dict, ...] = ()
    rows: tuple[DecisionRow, ...] = Field(min_length=1, max_length=10)
    targets: dict[Security, Quantity]
    reason: str = Field(min_length=1, max_length=1024)


class ExecutionRow(Contract):
    security: Security
    trading_date: date
    open: Money
    high: Money
    low: Money
    close: Money
    volume: Quantity
    observed_at: AwareDatetime
    complete: bool = Field(strict=True)
    source_watermark: date
    selected_watermark: date
    conflict: bool = Field(strict=True, default=False)

    @model_validator(mode="after")
    def prices(self):
        o, h, low, c = (Decimal(getattr(self, k)) for k in ("open", "high", "low", "close"))
        if not 0 < low <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid OHLC")
        return self


class CloseInput(Contract):
    schema_version: Literal["paper-close.v1"] = "paper-close.v1"
    trading_date: date
    intent_id: Sha
    data_manifest_sha256: Sha
    received_at: AwareDatetime
    deadline: AwareDatetime
    evidence_kind: Literal["synthetic", "historical_research"] = "synthetic"
    clock_basis: Literal["synthetic_clock", "retrospective_model"] = "synthetic_clock"
    rows: tuple[ExecutionRow, ...] = Field(min_length=1, max_length=10)


class ControlAction(Contract):
    reason: str = Field(min_length=1, max_length=1024)
    expected_parent: Sha | None


class AdvanceAction(ControlAction):
    day: date


class ForkSimulation(Contract):
    account_id: Identifier
    risk: RiskPolicy
