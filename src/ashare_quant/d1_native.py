"""Non-executing AD08 facade. Not registered as an RQAlpha DataSource.

This forwards the ACTUAL history call (including pre), never the caller's intended
get_prev_close argument. It does not derive previous dates, factors or values.
"""

from datetime import date, datetime, time
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .contracts import SHANGHAI, Contract, DomainError
from .d1_binding import ContractRehearsalBinding


class AD08Call(Contract):
    security: Literal["600000.XSHG"]
    trade_date: date
    history_dt: date
    adjust_orig: date
    frequency: Literal["1d"] = "1d"
    field: Literal["close"] = "close"
    bar_count: int = Field(default=1, strict=True, ge=1, le=1)
    include_now: bool = Field(default=False, strict=True)
    skip_suspended: bool = Field(default=False, strict=True)
    adjustment_requested: Literal["pre"] = "pre"

    @model_validator(mode="after")
    def no_argument_widening(self):
        if self.include_now or self.skip_suspended:
            raise ValueError("Only the documented AD08 shape is prepared")
        return self


def rq_date(value: date | datetime) -> date:
    """RQ naive midnight is a declared Shanghai model clock, not source visibility."""
    if isinstance(value, datetime):
        converted = value.astimezone(SHANGHAI) if value.tzinfo else value
        if converted.time().replace(tzinfo=None) != time(0):
            raise DomainError("D1_NATIVE_CLOCK_UNSUPPORTED", "AD08 native dates must be Shanghai model midnight", 422)
        return converted.date()
    if type(value) is date:
        return value
    raise DomainError("D1_NATIVE_CLOCK_UNSUPPORTED", "Expected a native date or model midnight", 422)


class NativeInstrumentIdentity(Protocol):
    order_book_id: str


class NativeAD08Preparation:
    """Explicit contract preparation only; no orders, accounts, broker or checkpoint."""

    def __init__(self, binding: ContractRehearsalBinding):
        self._binding = binding

    def prepare_history_call(self, instrument: NativeInstrumentIdentity, bar_count: int, frequency: str,
                             fields: str, dt: date | datetime, *, trade_date: date,
                             skip_suspended: bool = True, include_now: bool = False,
                             adjust_type: str = "pre", adjust_orig: date | datetime | None = None) -> AD08Call:
        if adjust_orig is None:
            raise DomainError("D1_NATIVE_ADJUST_ORIG_REQUIRED", "Preserve the actual RQ adjust_orig", 422)
        return AD08Call(security=instrument.order_book_id, trade_date=rq_date(trade_date),
                        history_dt=rq_date(dt), adjust_orig=rq_date(adjust_orig), frequency=frequency,
                        field=fields, bar_count=bar_count, include_now=include_now,
                        skip_suspended=skip_suspended, adjustment_requested=adjust_type)

    def rehearse_public_lookup(self, call: AD08Call) -> object:
        # DTO fields are dates, not manufactured timestamps or inferred trading dates.
        return self._binding.read_required("adjusted_prev_close", "adjusted_prev_close", **call.model_dump())

    def history_bars(self, *args, **kwargs):
        raise DomainError("D1_NATIVE_EXECUTION_NOT_ADMITTED", "This preparation facade is not a live RQ adapter", 422)
