"""Agent-facing types, input normalization, and bounds without hash/consent ceremony."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from ashare_quant.mcp.client import ResearchError
from ashare_quant.tasks.factors.contracts import EvaluationOptions, FactorCreate
from ashare_quant.tasks.minute.contracts import PythonCreate, PythonProject

Family = Literal["python_minute", "python_daily", "factors", "portfolio", "daily"]
Section = Literal[
    "trades",
    "positions",
    "equity",
    "orders",
    "strategy_logs",
    "analysis_series",
    "drawdowns",
    "monthly",
    "rolling",
    "factor_values",
    "evaluation",
    "evaluation_summary",
    "ic",
    "rank_ic",
    "quantile_membership",
    "quantile_returns",
    "spread",
    "stability",
    "turnover",
    "cost_sensitivity",
    "system_logs",
]
Library = Literal["alpha191", "qlib_alpha158", "joinquant"]
Definition = Annotated[str, Field(min_length=1, max_length=100)]
SourceID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Key = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")]
PageSize = Annotated[int, Field(ge=1, le=200)]
Cursor = Annotated[str, Field(max_length=1024)]
WaitSeconds = Annotated[float, Field(ge=0.0, le=120.0)]
Workspace = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]

CANONICAL_SECURITY_RE = re.compile(r"^(?:[0-9]{6}\.(?:XSHG|XSHE|XBSE|XBEI|BJ)|BJ\.[0-9]{6})$")


def normalize_security(raw: str) -> str:
    """Convert common A-share code notations (.SZ/.SH/sz/sh/6-digit) to canonical form."""
    if not isinstance(raw, str):
        raise ResearchError(
            "INVALID_SECURITY_CODE",
            f"Expected security code string, got {type(raw).__name__}",
            hint="Use codes like '000001.XSHE' (or '000001.SZ') and '600000.XSHG' (or '600000.SH').",
        )
    token = raw.strip().upper()
    m_suffix = re.fullmatch(r"([0-9]{6})\.(SZ|SH|SS|BJ)", token)
    if m_suffix:
        digits, market = m_suffix.groups()
        if market == "SZ":
            return f"{digits}.XSHE"
        if market in {"SH", "SS"}:
            return f"{digits}.XSHG"
        return f"BJ.{digits}"
    if CANONICAL_SECURITY_RE.fullmatch(token):
        return token
    m_prefix = re.fullmatch(r"(SZ|SH|SS|BJ)\.?([0-9]{6})", token)
    if m_prefix:
        market, digits = m_prefix.groups()
        if market == "SZ":
            return f"{digits}.XSHE"
        if market in {"SH", "SS"}:
            return f"{digits}.XSHG"
        return f"BJ.{digits}"
    if re.fullmatch(r"[0-9]{6}", token):
        if token.startswith("6"):
            return f"{token}.XSHG"
        if token.startswith(("0", "3")):
            return f"{token}.XSHE"
        if token.startswith(("4", "8", "92")):
            return f"BJ.{token}"
    raise ResearchError(
        "INVALID_SECURITY_CODE",
        f"Unrecognized security code '{raw}'",
        hint="Use '000001.XSHE' (or '000001.SZ'), '600000.XSHG' (or '600000.SH'), 'BJ.830799', or call search_securities(query=...).",
    )


def normalize_securities(values: list[str] | str | tuple[str, ...], *, allow_empty: bool = False) -> list[str]:
    if isinstance(values, str):
        raw_items = [item for item in re.split(r"[\s,]+", values.strip()) if item]
    elif isinstance(values, (list, tuple)):
        raw_items = list(values)
    else:
        raise ResearchError("INVALID_SECURITIES", "securities must be a list of codes or a code string")
    seen: list[str] = []
    for item in raw_items:
        code = normalize_security(item)
        if code not in seen:
            seen.append(code)
    if not seen and not allow_empty:
        raise ResearchError(
            "EMPTY_SECURITIES",
            "Select at least one security",
            hint="Pass e.g. securities=['000001.XSHE'] or call search_securities() first.",
        )
    return seen


def normalize_daily_window(start: str | date, end: str | date, *, max_days: int = 31) -> tuple[str, str]:
    try:
        s = date.fromisoformat(str(start))
        e = date.fromisoformat(str(end))
    except ValueError as exc:
        raise ResearchError(
            "INVALID_DATE_RANGE",
            f"Dates must be ISO YYYY-MM-DD (got start={start!r}, end={end!r})",
            hint="Use YYYY-MM-DD format, e.g. start='2024-03-01', end='2024-03-06'.",
        ) from exc
    if s > e:
        raise ResearchError(
            "INVALID_DATE_RANGE",
            f"start ({s}) must be on or before end ({e})",
        )
    if (e - s).days > max_days:
        suggested_end = (s + timedelta(days=max_days)).isoformat()
        raise ResearchError(
            "DATE_WINDOW_TOO_WIDE",
            f"Daily price inspection allows at most {max_days} calendar days per call (requested {(e - s).days} days)",
            hint=f"Split into <= {max_days}-day windows, e.g. start='{s.isoformat()}', end='{suggested_end}', or run a backtest/factor job for long horizons.",
        )
    return s.isoformat(), e.isoformat()


def normalize_minute_window(start: str, end: str, *, max_days: int = 3) -> tuple[str, str]:
    def _coerce(raw: str, *, is_end: bool) -> datetime:
        text = str(raw).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text = f"{text}T{'15:00:00' if is_end else '09:30:00'}+08:00"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", text):
            text = text.replace(" ", "T")
            if len(text) == 16:
                text += ":00"
            text += "+08:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ResearchError(
                "INVALID_MINUTE_TIMESTAMP",
                f"Invalid minute timestamp '{raw}'",
                hint="Use 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS+08:00', e.g. '2024-03-04T09:31:00+08:00'.",
            ) from exc
        if dt.tzinfo is None:
            raise ResearchError("INVALID_MINUTE_TIMESTAMP", f"Timestamp '{raw}' lacks timezone")
        return dt

    s_dt = _coerce(start, is_end=False)
    e_dt = _coerce(end, is_end=True)
    if s_dt > e_dt:
        raise ResearchError(
            "INVALID_DATE_RANGE",
            f"start ({s_dt.isoformat()}) must be on or before end ({e_dt.isoformat()})",
        )
    if e_dt - s_dt > timedelta(days=max_days):
        suggested_end = (s_dt + timedelta(days=max_days)).isoformat()
        raise ResearchError(
            "MINUTE_WINDOW_TOO_WIDE",
            f"Minute price queries allow at most {max_days} calendar days per call",
            hint=f"Query in <= {max_days}-day windows, e.g. start='{s_dt.isoformat()}', end='{suggested_end}'.",
        )
    return s_dt.isoformat(), e_dt.isoformat()


def auto_idempotency_key(prefix: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


__all__ = [
    "Cursor",
    "Definition",
    "EvaluationOptions",
    "FactorCreate",
    "Family",
    "Key",
    "Library",
    "PageSize",
    "PythonCreate",
    "PythonProject",
    "Section",
    "SourceID",
    "WaitSeconds",
    "Workspace",
    "auto_idempotency_key",
    "normalize_daily_window",
    "normalize_minute_window",
    "normalize_securities",
    "normalize_security",
]
