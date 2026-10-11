"""Select a contiguous execution prefix without changing the owner's read domain."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from ashare_quant.contracts import Contract, DomainError


def exact_date(value):
    if type(value) is date:
        return value
    if type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Use an exact YYYY-MM-DD trading date, not a timestamp")
    return value


ISODate = Annotated[date, BeforeValidator(exact_date)]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EndDateSelection(Contract):
    policy: Literal["admitted_window_prefix.v1"] = "admitted_window_prefix.v1"
    request_field: Literal["end_date"] = "end_date"
    inclusive: Literal[True] = True
    allowed_dates_source: Literal["windows[].scope.trading_dates"] = "windows[].scope.trading_dates"
    omitted_end: Literal["owner_window_end"] = "owner_window_end"
    start_date_selectable: Literal[False] = False
    creates_owner_data: Literal[False] = False


class ExecutionScope(Contract):
    schema_version: Literal["m2.execution-prefix.v1"] = "m2.execution-prefix.v1"
    owner_window_id: Literal["m2a", "w1", "w2"]
    parent_window_sha256: Sha
    requested_end_date: ISODate | None
    start_date: ISODate
    end_date: ISODate
    trading_dates: tuple[ISODate, ...] = Field(min_length=1, max_length=20)
    warmup_date: ISODate
    settlement_successor: ISODate


def selected_days(entry, window_id, end_date):
    days = entry.window(window_id).days
    if end_date is None:
        return days
    index = next((i for i, day in enumerate(days) if day.trading_date == end_date), None)
    if index is None:
        raise DomainError(
            "M2_END_DATE_NOT_ADMITTED",
            "Choose an exact trading date from the selected owner window; no rounding or extension",
            422,
        )
    return days[: index + 1]


def execution_days(entry, request):
    return selected_days(entry, request.window_id, request.end_date)


def resolve(entry, end_date):
    window = entry.windows[0]
    days = selected_days(entry, window.window_id, end_date)
    return ExecutionScope(
        owner_window_id=entry.owner_binding.owner_window_id,
        parent_window_sha256=window.window_id,
        requested_end_date=end_date,
        start_date=days[0].trading_date,
        end_date=days[-1].trading_date,
        trading_dates=tuple(day.trading_date for day in days),
        warmup_date=days[0].previous_open,
        settlement_successor=days[-1].next_open,
    )
