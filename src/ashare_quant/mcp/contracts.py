"""Agent-facing bounds; the backend still owns domain validation and consent."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ashare_quant.product.contracts import ProductCoverageRequest, ProductMinuteSelection
from ashare_quant.tasks.minute.contracts import PythonCreate

Family = Literal["python_minute", "portfolio", "daily"]
Section = Literal["summary", "trades", "positions", "equity", "orders", "strategy_logs"]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Key = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")]
PageSize = Annotated[int, Field(ge=1, le=200)]
Cursor = Annotated[str, Field(max_length=1024)]


class DailySelection(ProductCoverageRequest):
    @model_validator(mode="after")
    def agent_bounds(self):
        if len(self.securities) > 8 or (self.end - self.start).days > 31:
            raise ValueError("Choose at most 8 securities and 31 calendar days per query")
        return self


class MinuteSelection(ProductMinuteSelection):
    @model_validator(mode="after")
    def agent_bounds(self):
        if self.end - self.start > timedelta(days=3):
            raise ValueError("Choose at most 3 calendar days per minute query")
        return self


class StrategySubmission(PythonCreate):
    """Consent fields remain required Literal[True]; never inferred by this adapter."""
