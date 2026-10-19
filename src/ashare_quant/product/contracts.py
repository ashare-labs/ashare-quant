"""General market selection, independent of the fixed daily evaluation protocol."""

from datetime import date, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.backtests.daily.contracts import Sha
from ashare_quant.contracts import Contract, ErrorBody, MarketSecurity
from ashare_quant.tasks.portfolio.contracts import PortfolioDataset
from ashare_quant.tasks.result_contracts import JSONObject


class ProductSelection(Contract):
    securities: tuple[MarketSecurity, ...] = Field(min_length=1)
    start: date
    end: date
    source: str = Field(default="auto", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")

    @model_validator(mode="after")
    def bounds(self):
        if self.start > self.end or len(set(self.securities)) != len(self.securities):
            raise ValueError("Ordered dates and unique securities are required")
        return self


class ProductCoverageRequest(ProductSelection):
    reference_set: str | None = Field(default=None, min_length=1, max_length=80)


class ProductMinuteSelection(Contract):
    security: str = Field(pattern=r"^(?:[0-9]{6}\.(?:XSHG|XSHE)|BJ\.[0-9]{6})$")
    start: AwareDatetime
    end: AwareDatetime
    source: str = Field(default="auto", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")

    @model_validator(mode="after")
    def bounds(self):
        if (
            self.start > self.end
            or self.end - self.start > timedelta(days=60)
            or any(value.second or value.microsecond for value in (self.start, self.end))
        ):
            raise ValueError("Choose ordered, whole-minute timestamps spanning at most 60 days")
        return self


class ProductReleaseRequest(ProductCoverageRequest):
    name: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^\S(?:[^\r\n]*\S)?$"
    )
    mode: Literal["covered_research", "price_only_no_limits"] = "covered_research"


class ProductConfiguration(Contract):
    schema_version: Literal["product-configuration.v1"] = "product-configuration.v1"
    profile: str
    kind: str | None
    configured: bool
    configuration_id: Sha | None
    default_dataset_id: Sha | None
    assets: tuple[JSONObject, ...]
    capabilities: JSONObject
    reference_sets: tuple[JSONObject, ...] = ()
    default_reference_set: str | None = None
    limitations: tuple[str, ...]
    data_owner: JSONObject | None
    error: ErrorBody | None


class ProductRelease(Contract):
    release: JSONObject
    dataset: PortfolioDataset
