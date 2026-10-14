"""Separate forward-clock contracts; they cannot enter historical PaperService."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.contracts import Contract, Money

from .contracts import CloseInput, Decision, Security, SessionConfig, Sha
from .intraday_contracts import IntradayProof

CONDITIONAL_MODEL = "prior-close-conditional-intent-observed-open.v2"
INTRADAY_MODEL = "prior-close-intent-next-complete-minute.v1"
OnlineModel = Literal[
    "prior-close-standing-intent-observed-open.v1",
    "prior-close-conditional-intent-observed-open.v2",
    "prior-close-intent-next-complete-minute.v1",
]


class OnlineSession(SessionConfig):
    model: OnlineModel = "prior-close-standing-intent-observed-open.v1"
    data_mode: Literal["calendar_observed", "calendar_fixture"] = "calendar_observed"
    strategy_id: None = None
    replay_binding_sha256: None = None
    cash_dividend_policy: Literal["reject"] = "reject"
    admission_reader_sha256: Sha


class CalendarReplan(Contract):
    """An explicit finite replacement plan; it cannot supply market permission."""

    schema_version: Literal["paper-calendar-replan.v1"] = "paper-calendar-replan.v1"
    first_window: tuple[date, date]
    scope: tuple[Security, ...] = Field(min_length=1, max_length=3)
    strategy_id: Literal["equal-weight"] = "equal-weight"
    model: OnlineModel = "prior-close-standing-intent-observed-open.v1"
    previous_scenario_manifest_sha256: Sha
    admission_reader_sha256: Sha
    action_baseline_sha256: Sha
    calendar_source_sha256: Sha
    source_state_manifest_sha256: Sha

    @model_validator(mode="after")
    def bounded(self):
        first, last = self.first_window
        if (last - first).days != 1 or any(d.weekday() >= 5 for d in self.first_window):
            raise ValueError("This pilot replan requires two adjacent weekdays")
        if len(set(self.scope)) != len(self.scope):
            raise ValueError("Unique replan securities required")
        return self


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


class OpeningAdmissionRow(Contract):
    security: Security
    eligible: bool = Field(strict=True)
    suspended: bool = Field(strict=True)
    is_st: bool = Field(strict=True)
    reference_close: Money
    lower_limit: Money
    upper_limit: Money
    rule_version: str = Field(min_length=1)
    action_baseline_sha256: Sha
    action_review_sha256: Sha


class OpeningAdmission(Contract):
    schema_version: Literal["ad-calendar-phase-observation.v5", "ad-intraday-observation.v1"]
    capture_id: Sha
    observed_at: AwareDatetime
    rows: tuple[OpeningAdmissionRow, ...] = Field(min_length=1, max_length=3)


class OpeningInput(Contract):
    trading_date: date
    intent_id: Sha
    received_at: AwareDatetime
    evidence_kind: Literal["live_local_capture", "calendar_fixture"]
    clock_basis: Literal["wall_clock", "fixture_clock"]
    rows: tuple[OpeningRow, ...] = Field(min_length=1, max_length=3)
    execution_admission: OpeningAdmission | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    intraday: IntradayProof | None = Field(default=None, exclude_if=lambda v: v is None)
