"""Frozen daily observations for the shared Python callback/native account runtime."""

import json
from datetime import date
from pathlib import Path

from ashare_quant.backtests.minute.inputs import number, require, stamp
from ashare_quant.backtests.minute.research import (
    check_price,
    daily_model_bounds,
    digest,
    validate_state,
    validate_state_binding,
)
from ashare_quant.contracts import DomainError
from ashare_quant.portfolio.corporate import schedule_rows
from ashare_quant.providers.daily.history import frozen_factors, history_prices
from ashare_quant.storage.artifacts import atomic


def prepare_daily(metadata, daily, release, request, root, history_factors=None):
    document = {
        **metadata,
        "schema": "aq-python-daily-input.v1",
        "producer": {"name": "ashare-data", "api": "ReleasedDailyProduct.prices"},
        "daily": daily["rows"],
        "daily_receipt": daily["receipt"],
        "warmup_sessions": request.warmup_sessions,
        "history_factors": history_factors,
    }
    document["limitations"] = list(metadata["limitations"]) + [
        "Daily open execution uses retrospective daily volume as a capacity model",
        "Adjusted history uses an optional frozen owner factor snapshot, not account events",
        "Starting account is flat; in-run record-close holdings establish entitlements",
    ]
    document["research"] = {
        **metadata["research"],
        "capabilities": {
            "matching": "daily_open",
            "lifecycle": "daily",
            "capacity_policy": "daily",
            "initial_positions": "flat",
            "terminal_positions": "marked_at_final_close",
            "corporate_actions": "source_scoped_date_model",
        },
    }
    atomic(root / "input.json", document)
    inputs = DailyPythonInputs(root / "input.json")
    report = {
        **inputs.preflight(),
        "release": release.receipt(),
        "input_sha256": digest(document),
        "daily_receipt_sha256": digest(daily["receipt"]),
    }
    atomic(root / "preparation.json", report)
    return report


class DailyPythonInputs:
    frequency = "1d"
    research = True
    generic_research = False
    full_session_input = True
    session_data = None
    no_price_limits = False
    state_visibility = "posthoc_daily_replay"
    state_replay_used = True

    def __init__(self, path):
        self.document = doc = json.loads(Path(path).read_bytes())
        self.factors = frozen_factors(Path(path).parent, doc.get("history_factors"))
        self.sha256 = digest(doc)
        require(doc["schema"] == "aq-python-daily-input.v1", "PYTHON_DAILY_INPUT_SCHEMA")
        require(
            type(doc["real_market_data"]) is bool and doc["producer"]["name"] == "ashare-data",
            "PYTHON_DAILY_OWNER_REQUIRED",
        )
        require(
            doc["price_basis"] == "raw"
            and doc["volume_unit"] == "share"
            and doc["amount_unit"] == "CNY",
            "PYTHON_DAILY_UNITS",
        )
        require(doc["source_refs"] and doc["daily_receipt"], "PYTHON_DAILY_EVIDENCE_REQUIRED")
        self.ids = tuple(doc["securities"])
        require(
            0 < len(self.ids) <= 8 and len(self.ids) == len(set(self.ids)), "PYTHON_DAILY_UNIVERSE"
        )
        require(
            all(
                (s.endswith(".XSHG") and s[:3] in {"600", "601", "603", "605"})
                or (s.endswith(".XSHE") and s[:3] in {"000", "001", "002", "003"})
                for s in self.ids
            ),
            "PYTHON_DAILY_MAINBOARD_ONLY",
        )
        self.calendar, self.days = doc["calendar"], doc["days"]
        require(
            self.calendar == sorted(set(self.calendar)) and self.days and len(self.days) <= 126,
            "PYTHON_DAILY_CALENDAR",
        )
        for d in self.calendar:
            require(str(date.fromisoformat(d)) == d, "PYTHON_DAILY_DATE")
        first, last = self.calendar.index(self.days[0]), self.calendar.index(self.days[-1])
        require(
            first >= doc["warmup_sessions"] >= 1
            and last + 1 < len(self.calendar)
            and self.days == self.calendar[first : last + 1],
            "PYTHON_DAILY_WARMUP",
        )
        self.previous = {d: self.calendar[self.calendar.index(d) - 1] for d in self.days}
        self.rows = {}
        for row in doc["daily"]:
            key = row["security"], row["date"]
            require(
                key not in self.rows and key[0] in self.ids and key[1] in self.calendar[:-1],
                "PYTHON_DAILY_ROW_SCOPE",
            )
            fields = ("open", "high", "low", "close", "volume", "amount")
            if key[1] in self.days:
                fields += ("preclose",)
            for field in fields:
                try:
                    number(row.get(field), zero=field in {"volume", "amount"})
                except (ValueError, ArithmeticError) as exc:
                    raise DomainError(
                        "PYTHON_DAILY_VALUE_GAP",
                        "Missing or invalid daily value",
                        422,
                        details={"security": key[0], "date": key[1], "field": field},
                    ) from exc
            op, hi, lo, cl = (number(row[f]) for f in ("open", "high", "low", "close"))
            require(lo <= min(op, cl) <= max(op, cl) <= hi, "PYTHON_DAILY_OHLC")
            require(
                key[1] not in self.days
                or (row.get("tradestatus") in {"0", "1"} and row.get("is_st") in {"0", "1"}),
                "PYTHON_DAILY_STATE_REQUIRED",
            )
            self.rows[key] = row
        require(
            set(self.rows) == {(s, d) for s in self.ids for d in self.calendar[:-1]},
            "PYTHON_DAILY_GAPS",
        )
        self.states = {}
        for state in doc["states"]:
            sid, day = state["security"], state["date"]
            require(
                (sid, day) not in self.states and sid in self.ids and day in self.days,
                "PYTHON_DAILY_STATE_SCOPE",
            )
            validate_state(state, corporate=True, ordinary_only=False)
            validate_state_binding(state, doc, self.previous[day])
            row = self.row(sid, day)
            require(
                all(
                    row[k] == state["daily_limit_model"]["source_row"][k]
                    for k in ("open", "high", "low", "close", "volume", "amount", "preclose")
                ),
                "PYTHON_DAILY_STATE_ROW_CONFLICT",
            )
            require(
                number(state["previous_close"])
                == number(self.row(sid, self.previous[day])["close"]),
                "PYTHON_DAILY_PREVIOUS_CLOSE_CONFLICT",
            )
            if not state["suspended"]:
                check_price(state, row["low"], row["high"])
            self.states[sid, day] = state
        require(
            set(self.states) == {(s, d) for s in self.ids for d in self.days},
            "PYTHON_DAILY_STATE_GAPS",
        )
        self.corporate_actions = schedule_rows(
            doc, self.ids, {"start": self.days[0], "end": self.days[-1]}, self.calendar
        )
        require(self.corporate_actions is not None, "PYTHON_CORPORATE_GAPS")
        self.clocks = {d: [stamp(d + "T09:30:00+08:00")] for d in self.days}
        self.ends = self.clocks
        self.actions = []

    def row(self, sid, day):
        require((sid, day) in self.rows, "PYTHON_DAILY_ROW_UNAVAILABLE")
        row = self.rows[sid, day]
        state = getattr(self, "states", {}).get((sid, day))
        if state is not None:
            lower, upper = daily_model_bounds(state)
            return {**row, "lower_limit": lower, "upper_limit": upper}
        return row

    def state(self, sid, day, *, at=None):
        require((sid, day) in self.states, "PYTHON_DAILY_STATE_UNAVAILABLE")
        if at is not None:
            require(
                day <= str(at.date()) and stamp(day + "T09:00:00+08:00") <= at,
                "PYTHON_DAILY_STATE_NOT_VISIBLE",
            )
        return self.states[sid, day]

    def visible(self, sid, at, *, count=None, end=None, include_now=True, preserve_missing=False):
        require(sid in self.ids, "JQ_SECURITY_OUT_OF_SCOPE")
        cutoff = at if end is None else end
        require(cutoff <= at, "JQ_FUTURE_DATA_REQUEST")
        result = []
        for day in self.calendar[:-1]:
            available = stamp(day + "T15:00:00+08:00")
            if available > cutoff or (not include_now and available == at):
                continue
            result.append(
                {**self.row(sid, day), "_end": available, "bar_end": available.isoformat()}
            )
        return result[-count:] if count is not None else result

    def current(self, sid, at):
        day = str(at.date())
        if at.hour >= 15:
            return float(self.row(sid, day)["close"])
        if at.hour * 60 + at.minute >= 570:
            return float(self.row(sid, day)["open"])
        return float(self.row(sid, self.previous[day])["close"])

    def history_prices(self, rows, adjustment, basis_date):
        raw = [self.rows[r["security"], r["date"]] for r in rows]
        return history_prices(
            raw, self.document["daily_receipt"], self.factors, adjustment, basis_date
        )

    def is_paused(self, sid, day):
        value = self.rows[sid, day].get("tradestatus")
        if value not in {"0", "1"}:
            raise DomainError(
                "PYTHON_DAILY_HISTORY_STATE_GAP",
                "Pause filtering requires observed trading status; use skip_paused=False for raw rows",
                422,
                details={"security": sid, "date": day, "field": "tradestatus"},
            )
        return value == "0"

    valuation_price = current

    def check_order_price(self, sid, price, *, at):
        check_price(self.state(sid, str(at.date()), at=at), price, price)

    def price_bounds(self, sid, day, *, at):
        return daily_model_bounds(self.state(sid, day, at=at))

    def validate_execution(self, config):
        require(
            config["matching"] == "daily_open"
            and config["lifecycle"] == "daily"
            and config["capacity_policy"] == "daily",
            "PYTHON_DAILY_EXECUTION_MODEL",
        )

    def preflight(self):
        return {
            "ready": True,
            "frequency": "1d",
            "securities": list(self.ids),
            "trading_dates": self.days,
            "warmup_sessions": self.document["warmup_sessions"],
            "daily_rows": len(self.rows),
            "history_factors": self.document.get("history_factors"),
            "historical_pit_verified": False,
            "state_visibility": self.state_visibility,
            "corporate_schedule_sha256": digest(self.document["corporate_schedule"]),
            "limitations": self.document["limitations"],
        }
