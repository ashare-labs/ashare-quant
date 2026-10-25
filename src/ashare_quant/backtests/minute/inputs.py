"""Consumer validation only. AD owns acquisition, interval and state evidence."""

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from pathlib import Path

from ashare_quant.core.validation import digest, require
from ashare_quant.core.validation import number as number
from ashare_quant.core.validation import stamp as stamp


class MinuteInputs:
    """Immutable, complete declared sessions; no forward fill or guessed status.

    `assumed_closed` is an explicit research model, never verified historical PIT.
    Source labels and separate model clocks must already be supplied by AD.
    """

    def __init__(
        self, path, *, accept_assumed=False, accept_state_replay=False, accept_research=False
    ):
        import ashare_quant.backtests.minute.bounded as bounded
        import ashare_quant.backtests.minute.research as generic

        require(type(accept_assumed) is bool, "MINUTE_VISIBILITY_OPT_IN_MUST_BE_BOOLEAN")
        require(type(accept_state_replay) is bool, "MINUTE_STATE_OPT_IN_MUST_BE_BOOLEAN")
        require(type(accept_research) is bool, "MINUTE_RESEARCH_OPT_IN_MUST_BE_BOOLEAN")
        from ashare_quant.storage.streams import reading
        with reading(path) as stream:
            raw = stream.read()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.document = doc = json.loads(raw)
        self.generic_research = doc["schema"] == generic.SCHEMA
        research = generic if self.generic_research else bounded
        self._research_policy = research
        self.research = self.generic_research or doc["schema"] in {
            bounded.SCHEMA,
            bounded.SCHEMA_FULL,
        }
        self.full_session_input = doc["schema"] in {"ad-minute-backtest.v2", bounded.SCHEMA_FULL}
        require(
            self.research or doc["schema"] in {"ad-minute-backtest.v1", "ad-minute-backtest.v2"},
            "MINUTE_INPUT_SCHEMA",
        )
        require(not self.research or accept_research, "MINUTE_RESEARCH_REQUIRES_OPT_IN")
        if self.research:
            research.validate_document(doc)
        else:
            require("research" not in doc, "MINUTE_RESEARCH_SCHEMA_REQUIRED")
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
            bool(self.ids) and len(set(self.ids)) == len(self.ids),
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
        self.corporate_research = self.generic_research and research.is_corporate(doc)
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
            if self.research:
                if self.generic_research:
                    research.validate_state(state, corporate=self.corporate_research)
                else:
                    research.validate_state(state)
                if self.generic_research:
                    research.validate_state_binding(state, doc, self.previous[key[1]])
                else:
                    require(
                        state["evidence"]["binding_sha256"]
                        == doc["source_refs"]["state_binding_sha256"]
                        and state["tdx_previous_close"]["manifest_sha256"]
                        == doc["source_refs"]["tdx_manifest_sha256"][key[0]],
                        "MINUTE_RESEARCH_STATE_SOURCE_BINDING",
                    )
            else:
                require(
                    number(state["lower_limit"]) <= number(state["upper_limit"]),
                    "MINUTE_LIMIT_ORDER",
                )
            number(state["previous_close"])
            self.states[key] = state
        require(
            set(self.states) == {(s, d) for s in self.ids for d in self.days},
            "MINUTE_STATE_COVERAGE_GAP",
        )
        self.bars, self.by_security = {}, {s: [] for s in self.ids}
        for row in doc["minute"]:
            sid = row["security"]
            a, b = (
                research.interval(row)
                if self.research
                else (stamp(row["bar_start"]), stamp(row["bar_end"]))
            )
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
            amount = number(row["amount"], zero=True)
            use_policy = None
            if self.research:
                if self.generic_research:
                    use_policy = research.validate_bar_binding(row, doc)
                else:
                    require(
                        row["evidence"]["manifest_sha256"]
                        == doc["source_refs"]["tdx_manifest_sha256"][sid],
                        "MINUTE_RESEARCH_BAR_SOURCE_BINDING",
                    )
                if use_policy is None or use_policy["execution"] != "deny":
                    if self.generic_research:
                        research.validate_activity(
                            row, volume, amount,
                            full=self.full_session_input, corporate=self.corporate_research,
                        )
                    else:
                        research.validate_activity(row, volume, amount, full=self.full_session_input)
                    research.check_price(self.states[sid, d], row["low"], row["high"])
            require(not self.states[sid, d]["suspended"], "MINUTE_SUSPENDED_BAR_CONFLICT")
            self.bars[key] = {**row, "_start": a, "_end": b, "_available": available}
            if use_policy is not None:
                self.bars[key]["_use_policy"] = use_policy
            self.by_security[sid].append(self.bars[key])
        expected = {
            (s, t)
            for s in self.ids
            for d in self.days
            if not self.states[s, d]["suspended"]
            for t in self.ends[d]
        }
        self.gaps = (
            research.validate_gaps(doc, expected, set(self.bars)) if self.generic_research else {}
        )
        require(set(self.bars) | set(self.gaps) == expected, "MINUTE_BAR_COVERAGE_GAP")
        self.session_data = None
        if self.full_session_input:
            from ashare_quant.backtests.minute.session_data import SessionData

            self.session_data = SessionData(self, doc["session_observations"])
            for (sid, _), row in self.session_data.history.items():
                self.by_security[sid].append(row)
        else:
            require("session_observations" not in doc, "MINUTE_SESSION_INPUT_VERSION_REQUIRED")
        for rows in self.by_security.values():
            rows.sort(key=lambda r: r["_end"])
        self.labels = {s: [r["_end"] for r in rows] for s, rows in self.by_security.items()}
        # Strategy windows count declared time positions, including unavailable
        # quotes. Matching/valuation keep their existing, separately denied bars.
        self.history_by_security = {
            sid: [
                {
                    **row,
                    **dict.fromkeys(("open", "high", "low", "close", "volume", "amount")),
                    "missing_reason": "confirmed_invalid_price",
                }
                if "price_invalid" in row.get("_use_policy", {}).get("reasons", [])
                else row
                for row in rows
            ]
            for sid, rows in self.by_security.items()
        }
        for (sid, at), gap in self.gaps.items():
            self.history_by_security[sid].append(
                {
                    "security": sid,
                    "source_label": gap["source_label"],
                    "bar_end": at.isoformat(),
                    "_end": at,
                    "_available": at,
                    "missing_reason": gap["reason"],
                    **dict.fromkeys(("open", "high", "low", "close", "volume", "amount")),
                }
            )
        for rows in self.history_by_security.values():
            rows.sort(key=lambda r: r["_end"])
        self.history_labels = {
            s: [r["_end"] for r in rows] for s, rows in self.history_by_security.items()
        }
        self.day_gaps = {}
        for sid, at in self.gaps:
            self.day_gaps.setdefault((sid, at.date()), []).append(at)
        # Corporate actions remain AD-owned. Only the explicitly declared date
        # model consumes a schedule; older profiles still require no actions.
        require(doc["actions"] == [], "MINUTE_CORPORATE_ACTIONS_UNSUPPORTED")
        coverage = doc["action_coverage"]
        self.corporate_actions = None
        if self.research:
            self.corporate_actions = research.validate_actions(doc)
        else:
            require(
                coverage["status"] in {"source_covered_no_actions", "synthetic_known_no_actions"}
                and coverage["evidence"],
                "MINUTE_ACTION_COVERAGE_UNKNOWN",
            )
            require(
                not doc["real_market_data"] or coverage["status"] == "source_covered_no_actions",
                "MINUTE_REAL_ACTION_EVIDENCE_REQUIRED",
            )

    def price_bounds(self, sid, day, *, at):
        state = self.state(sid, day, at=at)
        if self.corporate_research:
            return self._research_policy.daily_model_bounds(state)
        if self.research:
            return self._research_policy.envelope(state["previous_close"])
        return number(state["lower_limit"]), number(state["upper_limit"])

    def check_order_price(self, sid, price, *, at):
        if self.research:
            self._research_policy.check_price(self.state(sid, str(at.date()), at=at), price, price)

    def validate_execution(self, settings):
        if self.generic_research:
            self._research_policy.validate_execution(settings)

    def visible(self, sid, at, *, include_now=True, count=None, end=None, preserve_missing=False):
        require(sid in self.ids, "MINUTE_SECURITY_OUT_OF_SCOPE")
        cutoff = at if end is None else end
        require(cutoff <= at, "AQUANT_FUTURE_DATA_REQUEST")
        # end bounds bar labels; at is the knowledge clock. include_now excludes
        # only the bar ending at the current clock, not an inclusive past end.
        labels = self.history_labels[sid] if preserve_missing else self.labels[sid]
        rows = self.history_by_security[sid] if preserve_missing else self.by_security[sid]
        index = (bisect_left if not include_now and cutoff == at else bisect_right)(labels, cutoff)
        result = []
        for i in range(index - 1, -1, -1):
            row = rows[i]
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
        if self.generic_research:
            return self.valuation_mark(sid, at)["price"]
        rows = self.visible(sid, at, count=1)
        today = [r for r in rows if r["_end"].date() == at.date()]
        state = self.state(sid, at.date().isoformat(), at=at)
        if not today and self.session_data is not None:
            opening = self.session_data.opening[sid, str(at.date())]
            if opening["_available"] <= at:
                return float(opening["price"])
        return float(today[-1]["close"] if today else state["previous_close"])

    def execution_denial(self, sid, at):
        if (sid, at) in self.gaps:
            return ["no_quote_in_selected_snapshot"]
        row = self.bars.get((sid, at))
        policy = row.get("_use_policy", {}) if row else {}
        return (
            policy.get("reasons", ["explicit_denial"]) if policy.get("execution") == "deny" else []
        )

    def valuation_mark(self, sid, at):
        """A stale account mark is never a new observation or a fill price."""
        # The dated previous-close observation is the new session's opening
        # reference. An earlier session's bounded minute slice may stop before
        # its close; it must not override that newer daily observation.
        start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        left, right = bisect_left(self.labels[sid], start), bisect_right(self.labels[sid], at)
        rows = [r for r in self.by_security[sid][left:right] if r["_available"] <= at]
        usable = [r for r in rows if r.get("_use_policy", {}).get("fresh_valuation") != "deny"]
        last = usable[-1] if usable else None
        current = self.bars.get((sid, at))
        denied = (current or {}).get("_use_policy", {}).get("fresh_valuation") == "deny"
        missing = (sid, at) in self.gaps
        # Also retain the stale designation between the denied/missing label and
        # the next usable observation, e.g. at after-close account snapshots.
        latest_bad = max(
            [t for t in self.day_gaps.get((sid, at.date()), ()) if t <= at]
            + [
                r["_end"] for r in rows if r.get("_use_policy", {}).get("fresh_valuation") == "deny"
            ],
            default=None,
        )
        stale = (
            denied
            or missing
            or (latest_bad is not None and (last is None or latest_bad > last["_end"]))
        )
        state = self.state(sid, str(at.date()), at=at)
        reference = state["previous_close"]
        reference_status = "previous_close_reference"
        if self.corporate_research and (at.hour, at.minute) >= (9, 30):
            reference = state["daily_limit_model"]["source_row"]["preclose"]
            reference_status = "daily_reference_model"
        return {
            "price": float(last["close"] if last else reference),
            "status": "stale" if stale else ("research_mark" if last else reference_status),
            "source_label": last["source_label"] if last else None,
            "last_unavailable_label": latest_bad.isoformat() if stale and latest_bad else None,
            "fresh_quote": bool(last is not None and last["_end"] == at and not stale),
        }

    def valuation_price(self, sid, at):
        if self.session_data is not None:
            closing = self.session_data.closing[sid, str(at.date())]
            if closing["_available"] <= at:
                return float(closing["price"])
        return self.current(sid, at)

    def boundary_execution_price(self, sid, at):
        """Qualify a JQ no-BAR price, independently of valuation's previous close."""
        require(sid in self.ids, "MINUTE_SECURITY_OUT_OF_SCOPE")
        clock = at.strftime("%H:%M")
        if clock == "09:30":
            require(self.session_data is not None, "AQUANT_OPENING_EXECUTION_PRICE_UNAVAILABLE")
            row = self.session_data.opening.get((sid, str(at.date())))
            require(
                row is not None and row["_available"] <= at,
                "AQUANT_OPENING_EXECUTION_PRICE_UNAVAILABLE",
            )
            return float(row["price"])
        require(clock == "13:00", "AQUANT_NO_BAR_EXECUTION_CLOCK_UNSUPPORTED")
        rows = self.visible(sid, at, count=1)
        require(
            rows and rows[-1]["_end"].date() == at.date(),
            "AQUANT_RESUME_EXECUTION_PRICE_UNAVAILABLE",
        )
        return float(rows[-1]["close"])

    def require_jq_capacity(self):
        require(self.session_data is not None, "AQUANT_DAILY_CAP_REQUIRES_AD_SESSION_OBSERVATIONS")

    def execution_daily_volume(self, sid, day):
        """Post-hoc execution denominator; never use this in a strategy data API."""
        self.require_jq_capacity()
        require(day in self.days and sid in self.ids, "AQUANT_DAILY_CAP_OUT_OF_SCOPE")
        return self.session_data.daily[sid, day]

    def preflight(self):
        result = {
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
        if self.research:
            result.update(
                execution_scope="bounded_retrospective_research",
                strict_execution_admitted=False,
                research=self.document["research"],
                action_coverage=self.document["action_coverage"],
                valuation_cutoff=(
                    "independent AD closing observation, subject to its visibility"
                    if self.full_session_input
                    else "14:56 source label, not official daily close"
                ),
                native_price_bounds="4% inward execution envelope; exchange limits unknown",
                requires_flat_end=not self.full_session_input,
            )
            if self.generic_research:
                if self.corporate_research:
                    result["native_price_bounds"] = (
                        "Explicit source daily-preclose 5%/10% model; exchange limits unknown"
                    )
                    result["corporate_schedule_sha256"] = digest(
                        self.document["corporate_schedule"]
                    )
                    result["corporate_account_model"] = self.document["corporate_account_model"]
                    result["zero_activity_policy"] = (
                        "Bound source zero volume/amount retained; zero execution capacity; "
                        "price is an uncertified research mark, not verified fresh trade data"
                    )
                result["valuation_cutoff"] = {
                    day: self.ends[day][-1].isoformat() for day in self.days
                }
                result["valuation_basis"] = "last source label under declared research clock"
                result["capabilities"] = self.document["research"]["capabilities"]
                result["quote_use_policy"] = {
                    "permission": "explicit_bounded_research_profile",
                    "hard_denials_override_permission": True,
                    "execution_denied_rows": sum(
                        r["_use_policy"]["execution"] == "deny" for r in self.bars.values()
                    ),
                    "fresh_valuation_denied_rows": sum(
                        r["_use_policy"]["fresh_valuation"] == "deny" for r in self.bars.values()
                    ),
                    "unavailable_slots": len(self.gaps),
                    "missing_quote_is_missing_trade": False,
                    "held_positions": "retain_quantity; last_usable_mark_explicitly_stale; never use stale mark for matching",
                }
        if self.session_data is not None:
            result["session_observations"] = {
                "schema": self.session_data.document["schema"],
                "history_rows": len(self.session_data.history),
                "opening_prices": len(self.session_data.opening),
                "closing_marks": len(self.session_data.closing),
                "daily_rows": len(self.session_data.daily),
                "source_refs": self.session_data.document["source_refs"],
                "limitations": self.session_data.document["limitations"],
            }
        return result


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
