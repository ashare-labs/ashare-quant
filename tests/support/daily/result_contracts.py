"""Shared current task result projections; money remains decimal text."""
from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from ashare_quant.contracts import Contract, Money
from tests.support.legacy_backtests.contracts import (
    AccountInput,
    NativeCallFact,
    Sha,
    Signal,
)
from tests.support.legacy_backtests.journal import NativeOrderProjection

GitOID = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SignedMoney = Annotated[str, Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]

WindowID = Literal["two_day", "w1", "w2"]
JSONObject = dict[str, JsonValue]



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


class M2Assumption(Contract):
    assumption_id: str
    owner: Literal["ashare-data", "ashare-quant"]
    description: str
    sha256: Sha
    version: str
