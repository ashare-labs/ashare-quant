"""Explicit conventions; no data access or implicit market defaults."""

import math
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


class AnalysisInputError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, kw_only=True)
class AnalysisPolicy:
    annualization: int
    risk_free_annual: float | None
    risk_free_source: str
    min_risk_observations: int

    def __post_init__(self):
        if type(self.annualization) is not int or not 1 <= self.annualization <= 366:
            raise AnalysisInputError("invalid_annualization")
        if (type(self.min_risk_observations) is not int
                or not 2 <= self.min_risk_observations <= 2520):
            raise AnalysisInputError("invalid_minimum_observations")
        if self.risk_free_annual is not None:
            if number(self.risk_free_annual) is None or self.risk_free_annual <= -1:
                raise AnalysisInputError("invalid_risk_free_rate")
        if not isinstance(self.risk_free_source, str) or not self.risk_free_source.strip():
            raise AnalysisInputError("missing_risk_free_source")


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def session(value):
    if not isinstance(value, str):
        raise AnalysisInputError("invalid_account_date")
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisInputError("invalid_account_date") from exc
    if moment.tzinfo is None:
        raise AnalysisInputError("missing_account_timezone")
    return moment.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def metric(value=None, *, unit="ratio", reason=None, count=0):
    value = number(value)
    if value is None and reason is None:
        reason = "non_finite_result"
    if reason is not None:
        value = None
    return {"value": value, "unit": unit, "status": "ok" if reason is None else "unavailable",
            "reason": reason, "sample_count": count}
