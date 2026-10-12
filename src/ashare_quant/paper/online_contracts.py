"""Separate forward-clock contracts; they cannot enter historical PaperService."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from ashare_quant.contracts import Contract, Money

from .contracts import CloseInput, Decision, Security, SessionConfig, Sha


class OnlineSession(SessionConfig):
    model: Literal["prior-close-standing-intent-observed-open.v1"] = (
        "prior-close-standing-intent-observed-open.v1"
    )
    data_mode: Literal["calendar_observed", "calendar_fixture"] = "calendar_observed"
    strategy_id: None = None
    replay_binding_sha256: None = None
    cash_dividend_policy: Literal["reject"] = "reject"
    admission_reader_sha256: Sha


class OnlineDecision(Decision):
    evidence_kind: Literal["live_local_capture", "calendar_fixture"]
    clock_basis: Literal["wall_clock", "fixture_clock"]


class OnlineClose(CloseInput):
    evidence_kind: Literal["live_local_capture", "calendar_fixture"]
    clock_basis: Literal["wall_clock", "fixture_clock"]


class OpeningRow(Contract):
    security: Security
    trading_date: date
    open: Money
    observed_at: AwareDatetime
    source_as_of: AwareDatetime
    raw_sha256: Sha


class OpeningInput(Contract):
    trading_date: date
    intent_id: Sha
    received_at: AwareDatetime
    evidence_kind: Literal["live_local_capture", "calendar_fixture"]
    clock_basis: Literal["wall_clock", "fixture_clock"]
    rows: tuple[OpeningRow, ...] = Field(min_length=1, max_length=3)
