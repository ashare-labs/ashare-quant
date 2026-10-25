"""Shared wire shapes. Distinct historical schema spellings stay distinct."""

from datetime import date
from typing import Annotated

from pydantic import BeforeValidator, Field, JsonValue

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Paper/diagnostics publish this spelling; preserving it avoids schema churn.
EvidenceSha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ResearchSecurity = Annotated[str, Field(pattern=r"^[0-9]{6}\.(XSHG|XSHE)$")]
JSONObject = dict[str, JsonValue]


def exact_date(value):
    if type(value) is date:
        return value
    if type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Use an exact YYYY-MM-DD trading date, not a timestamp")
    return value


ISODate = Annotated[date, BeforeValidator(exact_date)]
