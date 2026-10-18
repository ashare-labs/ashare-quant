"""General market selection, independent of the fixed daily evaluation protocol."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

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


class ProductReleaseRequest(ProductSelection):
    name: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^\S(?:[^\r\n]*\S)?$")


class ProductConfiguration(Contract):
    schema_version: Literal["product-configuration.v1"] = "product-configuration.v1"
    profile: str
    kind: str | None
    configured: bool
    configuration_id: Sha | None
    default_dataset_id: Sha | None
    assets: tuple[JSONObject, ...]
    capabilities: JSONObject
    limitations: tuple[str, ...]
    data_owner: JSONObject | None
    error: ErrorBody | None


class ProductRelease(Contract):
    release: JSONObject
    dataset: PortfolioDataset
