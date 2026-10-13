"""Consumer validation only. AD owns acquisition, interval and state evidence."""

import hashlib
import json
import re
import sys
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ashare_quant.contracts import SHANGHAI
from ashare_quant.daily_portfolio.inputs import digest, require


def stamp(value):
    require(isinstance(value, str), "MINUTE_AWARE_TIMESTAMP_REQUIRED")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "MINUTE_AWARE_TIMESTAMP_REQUIRED")
    return result.astimezone(SHANGHAI)


def number(value, *, zero=False):
    require(type(value) in (str, int, float), "MINUTE_NUMBER_TYPE")
    n = Decimal(str(value))
    require(
        n.is_finite() and (n >= 0 if zero else n > 0) and n < Decimal("1e15"),
        "MINUTE_INVALID_NUMBER",
    )
    require(n == 0 or float(n) >= sys.float_info.min, "MINUTE_NUMBER_UNDERFLOW")
    return n


class MinuteInputs:
    """Immutable, complete declared sessions; no forward fill or guessed status.

    `assumed_closed` is an explicit research model, never verified historical PIT.
    Actual interval labels must already be supplied by AD, even in this mode.
    """

    def __init__(self, path, *, accept_assumed=False, accept_state_replay=False):
        require(type(accept_assumed) is bool, "MINUTE_VISIBILITY_OPT_IN_MUST_BE_BOOLEAN")
        require(type(accept_state_replay) is bool, "MINUTE_STATE_OPT_IN_MUST_BE_BOOLEAN")
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.document = doc = json.loads(raw)
        require(doc["schema"] == "ad-minute-backtest.v1", "MINUTE_INPUT_SCHEMA")
        require(type(doc["real_market_data"]) is bool, "MINUTE_REAL_FLAG_REQUIRED")
        require(
            not doc["real_market_data"] or doc["producer"]["name"] == "ashare-data",
            "MINUTE_REAL_INPUT_REQUIRES_AD",
        )
        require(
            doc["source_refs"] and isinstance(doc["limitations"], list),
            "MINUTE_SOURCE_EVIDENCE_REQUIRED",
        )
        require(
            doc["price_basis"] == "raw"
            and doc["volume_unit"] == "share"
            and doc["amount_unit"] == "CNY",
            "MINUTE_RAW_SHARE_CNY_REQUIRED",
        )
        self.visibility = doc["visibility"]
        require(
            self.visibility in {"declared_available_at", "assumed_closed"},
            "MINUTE_VISIBILITY_MODEL_REQUIRED",
        )
        require(
            self.visibility != "assumed_closed" or accept_assumed,
            "MINUTE_ASSUMED_VISIBILITY_REQUIRES_OPT_IN",
        )
        self.ids = tuple(doc["securities"])
        require(
            0 < len(self.ids) <= 20 and len(set(self.ids)) == len(self.ids),
            "MINUTE_BOUNDED_UNIVERSE_REQUIRED",
        )
        require(
            all(re.fullmatch(r"(?:60\d{4}\.XSHG|00\d{4}\.XSHE)", s) for s in self.ids),
            "MINUTE_ONLY_MAINBOARD_STOCKS",
        )
        self.calendar = doc["calendar"]
        require(self.calendar == sorted(set(self.calendar)), "MINUTE_CALENDAR_ORDER")
        for d in self.calendar:
            require(date.fromisoformat(d).isoformat() == d, "MINUTE_CALENDAR_DATE")
        self.days = doc["days"]
        require(self.days and set(self.days) <= set(self.calendar), "MINUTE_WINDOW_CALENDAR")
        first, last = self.calendar.index(self.days[0]), self.calendar.index(self.days[-1])
        require(
            first > 0
            and last + 1 < len(self.calendar)
            and self.days == self.calendar[first : last + 1],
            "MINUTE_PREVIOUS_SUCCESSOR_AND_CONTIGUOUS_DAYS_REQUIRED",
        )
        require(len(self.days) <= 60, "MINUTE_WINDOW_TOO_LARGE")
        self.previous = {d: self.calendar[self.calendar.index(d) - 1] for d in self.days}
        self.sessions, self.clocks, self.ends = {}, {}, {}
        for d in self.days:
            sessions = [(stamp(a), stamp(b)) for a, b in doc["sessions"][d]]
            require(sessions and sessions == sorted(sessions), "MINUTE_SESSION_ORDER")
            for a, b in sessions:
                require(
                    a.date().isoformat() == b.date().isoformat() == d
                    and a < b
                    and not (a.second or b.second or a.microsecond or b.microsecond)
                    and "09:30" <= a.strftime("%H:%M") < b.strftime("%H:%M") <= "15:00"
                    and not (a.strftime("%H:%M") < "13:00" and b.strftime("%H:%M") > "11:30"),
                    "MINUTE_SESSION_OUTSIDE_SUPPORTED_CASH_HOURS",
                )
            require(
                all(a[1] < b[0] for a, b in zip(sessions, sessions[1:])), "MINUTE_SESSION_OVERLAP"
            )
            self.sessions[d] = sessions
            self.ends[d] = tuple(
                a + timedelta(minutes=n)
                for a, b in sessions
                for n in range(1, int((b - a).total_seconds() / 60) + 1)
            )
            self.clocks[d] = tuple(sorted({*self.ends[d], *(a for a, _ in sessions)}))
        self.state_visibility = doc.get("state_visibility", "declared_available_at")
        require(
            self.state_visibility in {"declared_available_at", "posthoc_daily_replay"},
            "MINUTE_STATE_VISIBILITY_MODEL_REQUIRED",
        )
        self.state_replay_used = self.state_visibility == "posthoc_daily_replay"
        require(
            not self.state_replay_used or accept_state_replay,
            "MINUTE_STATE_REPLAY_REQUIRES_OPT_IN",
        )
        if self.state_replay_used:
            model = doc.get("state_replay", {})
            require(
                model.get("model") == "dated-daily-state-0900.v1"
                and model.get("purpose") == "retrospective_research"
                and model.get("evidence")
                and isinstance(model.get("limitations"), list)
                and model["limitations"],
                "MINUTE_STATE_REPLAY_EVIDENCE_REQUIRED",
            )
        else:
            require("state_replay" not in doc, "MINUTE_STATE_REPLAY_MODE_NOT_DECLARED")
        self.states, self.state_times = {}, {}
        for state in doc["states"]:
            key = (state["security"], state["date"])
            require(
                key not in self.states and key[0] in self.ids and key[1] in self.days,
                "MINUTE_STATE_IDENTITY",
            )
            require(
                type(state["suspended"]) is bool and state["listed"] is True,
                "MINUTE_EXPLICIT_STATE_REQUIRED",
            )
            require(state.get("is_st") is None or type(state["is_st"]) is bool, "MINUTE_ST_TYPE")
            require(state["evidence"], "MINUTE_STATE_EVIDENCE_REQUIRED")
            opening = stamp(key[1] + "T09:00:00+08:00")
            if self.state_replay_used:
                # Keep genuine historical availability null in the frozen input.
                # A dated research assumption has a separate clock and receipt.
                require(
                    "available_at" in state
                    and state["available_at"] is None
                    and state.get("historical_available_at") is None,
                    "MINUTE_STATE_REPLAY_MUST_PRESERVE_UNKNOWN_AVAILABILITY",
                )
                require(
                    stamp(state.get("replay_effective_at")) == opening,
                    "MINUTE_STATE_REPLAY_CLOCK_MUST_BE_SAME_DAY_0900",
                )
                stamp(state.get("observed_at"))
                self.state_times[key] = opening
            else:
                require(
                    state.get("available_at") is not None,
                    "MINUTE_STATE_AVAILABILITY_UNKNOWN",
                )
                require(
                    "replay_effective_at" not in state,
                    "MINUTE_STATE_REPLAY_MODE_NOT_DECLARED",
                )
                available = stamp(state["available_at"])
                if "historical_available_at" in state:
                    require(
                        state["historical_available_at"] is not None
                        and stamp(state["historical_available_at"]) == available,
                        "MINUTE_STATE_AVAILABILITY_CONFLICT",
                    )
                require(available <= opening, "MINUTE_STATE_NOT_AVAILABLE_BEFORE_OPEN")
                self.state_times[key] = available
            require(
                number(state["lower_limit"]) <= number(state["upper_limit"]), "MINUTE_LIMIT_ORDER"
            )
            number(state["previous_close"])
            self.states[key] = state
        require(
            set(self.states) == {(s, d) for s in self.ids for d in self.days},
            "MINUTE_STATE_COVERAGE_GAP",
        )
        self.bars, self.by_security = {}, {s: [] for s in self.ids}
        for row in doc["minute"]:
            sid, a, b = row["security"], stamp(row["bar_start"]), stamp(row["bar_end"])
            d = b.date().isoformat()
            require(
                sid in self.ids
                and d in self.days
                and b in self.ends[d]
                and b - a == timedelta(minutes=1),
                "MINUTE_BAR_INTERVAL",
            )
            key = (sid, b)
            require(key not in self.bars, "MINUTE_DUPLICATE_BAR")
            require(row["evidence"], "MINUTE_BAR_EVIDENCE_REQUIRED")
            if self.visibility == "declared_available_at":
                require(row.get("available_at") is not None, "MINUTE_BAR_AVAILABILITY_UNKNOWN")
            available = stamp(row["available_at"]) if row.get("available_at") else b
            require(available >= b, "MINUTE_UNFINISHED_BAR_VISIBLE")
            o, h, low, c = (number(row[f]) for f in ("open", "high", "low", "close"))
            require(low <= min(o, c) <= max(o, c) <= h, "MINUTE_INVALID_OHLC")
            volume = number(row["volume"], zero=True)
            require(volume == volume.to_integral_value(), "MINUTE_VOLUME_NOT_SHARES")
            number(row["amount"], zero=True)
            require(not self.states[sid, d]["suspended"], "MINUTE_SUSPENDED_BAR_CONFLICT")
            self.bars[key] = {**row, "_start": a, "_end": b, "_available": available}
            self.by_security[sid].append(self.bars[key])
        expected = {
            (s, t)
            for s in self.ids
            for d in self.days
            if not self.states[s, d]["suspended"]
            for t in self.ends[d]
        }
        require(set(self.bars) == expected, "MINUTE_BAR_COVERAGE_GAP")
        for rows in self.by_security.values():
            rows.sort(key=lambda r: r["_end"])
        self.labels = {s: [r["_end"] for r in rows] for s, rows in self.by_security.items()}
        # Corporate actions remain AD-owned; this first execution scope requires
        # explicit no-action coverage, never an absent key interpreted as none.
        require(doc["actions"] == [], "MINUTE_CORPORATE_ACTIONS_UNSUPPORTED")
        coverage = doc["action_coverage"]
        require(
            coverage["status"] in {"source_covered_no_actions", "synthetic_known_no_actions"}
            and coverage["evidence"],
            "MINUTE_ACTION_COVERAGE_UNKNOWN",
        )
        require(
            not doc["real_market_data"] or coverage["status"] == "source_covered_no_actions",
            "MINUTE_REAL_ACTION_EVIDENCE_REQUIRED",
        )

    def visible(self, sid, at, *, include_now=True, count=None, end=None):
        require(sid in self.ids, "MINUTE_SECURITY_OUT_OF_SCOPE")
        cutoff = at if end is None else end
        require(cutoff <= at, "JQ_FUTURE_DATA_REQUEST")
        # end bounds bar labels; at is the knowledge clock. include_now excludes
        # only the bar ending at the current clock, not an inclusive past end.
        index = (bisect_left if not include_now and cutoff == at else bisect_right)(
            self.labels[sid], cutoff
        )
        result = []
        for i in range(index - 1, -1, -1):
            row = self.by_security[sid][i]
            if row["_available"] <= at:
                result.append(row)
                if count is not None and len(result) >= count:
                    break
        return result[::-1]

    def state(self, sid, day, *, at=None):
        require((sid, day) in self.states, "MINUTE_STATE_OUT_OF_SCOPE")
        if at is not None:
            require(
                day <= at.date().isoformat() and self.state_times[sid, day] <= at,
                "MINUTE_STATE_NOT_VISIBLE_AT_CLOCK",
            )
        return self.states[sid, day]

    def current(self, sid, at):
        rows = self.visible(sid, at, count=1)
        today = [r for r in rows if r["_end"].date() == at.date()]
        state = self.state(sid, at.date().isoformat(), at=at)
        return float(today[-1]["close"] if today else state["previous_close"])

    def preflight(self):
        return {
            "input_sha256": self.sha256,
            "securities": list(self.ids),
            "days": self.days,
            "minute_rows": len(self.bars),
            "visibility": self.visibility,
            "state_visibility": self.state_visibility,
            "state_replay_used": self.state_replay_used,
            "state_replay": self.document.get("state_replay"),
            "real_market_data": self.document["real_market_data"],
            "historical_pit_verified": False,
            "jq_comparison": "NOT_PERFORMED",
            "limitations": self.document["limitations"],
            "ready": True,
        }


def export_ad_snapshot(
    snapshot, metadata, path, *, accept_assumed=False, accept_state_replay=False
):
    """Project public AD Snapshot.bars output, preserving its supplied intervals.

    Metadata is an AD-owned state/calendar/action delivery. This function does not
    download, normalize a source label, infer a state, or fill a missing minute.
    """
    doc = json.loads(json.dumps(metadata))
    require(doc["producer"]["name"] == "ashare-data", "MINUTE_AD_METADATA_REQUIRED")
    doc["minute"] = []
    for day in doc["days"]:
        for start, end in doc["sessions"][day]:
            read = snapshot.bars(
                doc["securities"],
                start,
                end,
                strict=True,
                quality="observed" if doc["real_market_data"] else "synthetic",
                as_of=end,
                adjustment=None,
                frequency="1m",
            )
            require(
                read["units"] == {"price": "CNY", "volume": "shares", "amount": "CNY"},
                "MINUTE_AD_UNITS",
            )
            for row in read["rows"]:
                doc["minute"].append(
                    {
                        "security": row["symbol"],
                        **{
                            k: str(row[k])
                            for k in ("open", "high", "low", "close", "volume", "amount")
                        },
                        **{k: row[k] for k in ("bar_start", "bar_end", "available_at")},
                        "evidence": {
                            "snapshot_id": read["snapshot_id"],
                            "source_id": row["source_id"],
                            "row_sha256": digest(json.loads(json.dumps(row, default=str))),
                        },
                    }
                )
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2))
    return MinuteInputs(
        path, accept_assumed=accept_assumed, accept_state_replay=accept_state_replay
    )
