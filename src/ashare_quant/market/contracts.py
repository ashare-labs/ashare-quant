"""Immutable market publication descriptors and selection preflight."""
from datetime import date as CalendarDate
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ashare_quant.contracts import Contract
from ashare_quant.tasks.contracts import JSONObject, Security, Sha


class PortfolioSelection(Contract):
    dataset_id: Sha
    securities: tuple[Security, ...] = Field(min_length=1)
    start_date: CalendarDate
    end_date: CalendarDate
    accept_retrospective_state: bool = False
    mode: Literal["covered_research", "price_only_no_limits"] = "covered_research"

    @model_validator(mode="after")
    def bounds(self):
        if self.start_date > self.end_date or len(set(self.securities)) != len(self.securities):
            raise ValueError("Ordered dates and unique securities are required")
        return self

    @field_validator("securities")
    @classmethod
    def ordered(cls, values):
        return tuple(sorted(values))



class PortfolioScope(Contract):
    securities: tuple[Security, ...]
    start_date: CalendarDate
    end_date: CalendarDate
    trading_dates: tuple[CalendarDate, ...]
    warmup_sessions: Literal[120] = 120
    frequency: Literal["1d"] = "1d"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"



class PortfolioPreflight(Contract):
    selection: PortfolioSelection
    ready: bool
    gaps: tuple[JSONObject, ...]
    assumptions: tuple[JSONObject | str, ...]
    limitations: tuple[str, ...]
    economic_scope: str
    real_market_data: bool
    historical_pit_verified: Literal[False] = False
    input_sha256: Sha | None
    assumption_ack_sha256: Sha
    scope: PortfolioScope | None



class PortfolioDataset(Contract):
    dataset_id: Sha
    name: str
    input_sha256: Sha
    securities: tuple[Security, ...]
    trading_dates: tuple[CalendarDate, ...]
    real_market_data: bool
    state_mode: str
    action_coverage: JSONObject
    source_refs: tuple[JSONObject | str, ...]
    limitations: tuple[str, ...]
    mode: Literal["covered_research", "price_only_no_limits"] = "covered_research"
    diagnostic_allowed: bool = True
