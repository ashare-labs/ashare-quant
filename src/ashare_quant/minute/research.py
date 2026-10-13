"""Bounded opt-in research admission; never promote AD's strict evidence flags.

The native broker still checks cash, T+1, volume and price. Its price board uses
an inward 4% execution envelope, NOT a claimed exchange price limit. Source
limits/reference/intervals/availability remain null. Other research profiles are
deliberately unsupported until their data and assumptions have been reviewed.
"""

from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .inputs import number, require, stamp

SCHEMA = "aq-tdx-minute-research.v1"
SCHEMA_FULL = "aq-tdx-minute-research.v2"
PROFILE = "tdx-two-bank-20261009-12.v1"
PROFILE_FULL = "tdx-two-bank-20261009-12.full-session.v1"
IDS = ("600000.XSHG", "000001.XSHE")
DAYS = ["2026-10-09", "2026-10-12"]
CALENDAR = ["2026-10-08", *DAYS, "2026-10-13"]
SESSIONS = [["09:35", "11:30"], ["13:00", "14:56"]]
ASSUMPTIONS = [
    "source_label_is_interval_end_excluding_opening_and_closing_auctions",
    "bar_visible_at_model_interval_end_not_observed_historical_availability",
    "dated_source_states_replayed_at_0900_not_observed_historical_availability",
    "daily_tradable_status_and_positive_minute_volume_no_unreported_intraday_halt",
    "bounded_action_sources_have_no_omitted_action_affecting_new_positions",
    "no_ex_right_reference_change_within_the_selected_no_known_action_window",
    "source_float_encoded_share_volume_and_CNY_amount_used_without_precision_repair",
]
FULL_SESSIONS = [["09:30", "11:30"], ["13:00", "15:00"]]
FULL_ASSUMPTIONS = [
    "source_label_is_interval_end_including_auction_rows_for_explicit_jq_research",
    *ASSUMPTIONS[1:3],
    "daily_tradable_status_with_AD_declared_zero_volume_closing_phase_rows_not_intraday_halt_proof",
    *ASSUMPTIONS[4:],
    "opening_proxy_and_close_valuation_roles_are_AD_supplied_not_historical_PIT",
    "terminal_holdings_are_marked_only_no_post_window_corporate_action_claim",
]


def validate_activity(row, volume, amount, *, full):
    """Zero activity is admissible only in an explicitly evidenced closing role.

    This classifies a bounded research interval, not historical exchange phases
    or an absence of intraday halts. Source volume/amount are never rewritten.
    """
    if volume > 0:
        require(amount > 0, "MINUTE_INVALID_NUMBER")
        return
    require(full, "MINUTE_RESEARCH_ZERO_VOLUME_HALT_AMBIGUITY")
    require(amount == 0, "MINUTE_RESEARCH_ZERO_VOLUME_AMOUNT_CONFLICT")
    phase = row.get("phase_evidence")
    require(
        row.get("session_phase") == "closing_auction"
        and "14:57" < stamp(row["source_label"]).strftime("%H:%M") <= "15:00"
        and isinstance(phase, dict),
        "MINUTE_RESEARCH_ZERO_VOLUME_PHASE_REQUIRED",
    )
    require(
        phase.get("model") == "ad-declared-closing-stage.v1"
        and phase.get("source_label") == row["source_label"]
        and phase.get("manifest_sha256") == row["evidence"]["manifest_sha256"]
        and phase.get("historical_phase_verified") is False
        and isinstance(phase.get("source_refs"), list)
        and phase["source_refs"]
        and all(isinstance(s, str) and s for s in phase["source_refs"]),
        "MINUTE_RESEARCH_ZERO_VOLUME_PHASE_EVIDENCE",
    )


def envelope(previous_close):
    """An execution restriction around an observed close, not a reference-price fact."""
    p = number(previous_close)
    return (
        (p * Decimal("0.96")).quantize(Decimal("0.01"), rounding=ROUND_CEILING),
        (p * Decimal("1.04")).quantize(Decimal("0.01"), rounding=ROUND_FLOOR),
    )


def validate_document(doc):
    model = doc["research"]
    full = doc["schema"] == SCHEMA_FULL
    require(
        doc["real_market_data"] is True
        and model["profile"] == (PROFILE_FULL if full else PROFILE)
        and model["purpose"] == "retrospective_research"
        and model["assumptions"] == (FULL_ASSUMPTIONS if full else ASSUMPTIONS)
        and model["strict_execution_admitted"] is False
        and model["interval_verified"] is False
        and model["price_limit_verified"] is False
        and model["envelope"] == "raw-previous-close-4pct-inward-cent.v1",
        "MINUTE_RESEARCH_PROFILE_REQUIRED",
    )
    require(
        tuple(doc["securities"]) == IDS
        and doc["days"] == DAYS
        and doc["calendar"] == CALENDAR
        and doc["sessions"]
        == {
            d: [
                [d + "T" + a + ":00+08:00", d + "T" + b + ":00+08:00"]
                for a, b in (FULL_SESSIONS if full else SESSIONS)
            ]
            for d in DAYS
        },
        "MINUTE_RESEARCH_FIXED_CASE_SCOPE",
    )
    require(
        doc["visibility"] == "assumed_closed" and doc["state_visibility"] == "posthoc_daily_replay",
        "MINUTE_RESEARCH_SEPARATE_REPLAY_CLOCKS_REQUIRED",
    )
    # Preserve the strict owner's refusal alongside this *different* research case.
    strict = model["original_admission"]
    require(
        strict["ready_for_execution"] is False
        and strict["real_backtest_executed"] is False
        and strict["blocking_gates"],
        "MINUTE_RESEARCH_CANNOT_PROMOTE_STRICT_ADMISSION",
    )
    require(set(model["tdx_reports"]) == set(IDS), "MINUTE_RESEARCH_SOURCE_COVERAGE")
    for sid, report in model["tdx_reports"].items():
        require(
            report["request"]["security"] == sid
            and report["request"]["frequency"] == "1m"
            and report["source"] == "tdx_native_0x052d"
            and report["manifest_sha256"] == doc["source_refs"]["tdx_manifest_sha256"][sid]
            and report["historical_available_at"] is None
            and report["bar_start"] is None
            and report["bar_end"] is None
            and report["historical_pit"] is False
            and report["execution_admitted"] is False
            and report["interval_verified"] is False
            and report["corporate_actions_verified"] is False
            and report["trading_status_verified"] is False,
            "MINUTE_RESEARCH_SOURCE_FLAGS_CHANGED",
        )
    calendars = model["calendars"]
    for exchange in ("XSHG", "XSHE"):
        require(
            calendars[exchange]["source_refs"]
            and calendars[exchange]["days"]
            == {
                "2026-10-08": True,
                "2026-10-09": True,
                "2026-10-10": False,
                "2026-10-11": False,
                "2026-10-12": True,
                "2026-10-13": True,
            },
            "MINUTE_RESEARCH_CALENDAR_EVIDENCE",
        )


def validate_state(state):
    observed = state["source_observation"]
    require(
        observed["security"] == state["security"]
        and observed["date"] == state["date"]
        and observed["historical_available_at"] is None
        and observed["replay_effective_at"] is None
        and observed["intraday_halts_verified"] is False
        and observed["dependency_conflicts"] == []
        and observed["source_quality"]["execution_permission"] is False,
        "MINUTE_RESEARCH_STATE_IDENTITY",
    )
    for field in ("listed", "suspended", "is_st", "previous_close"):
        require(
            observed[field]["value"] == state[field]
            and observed[field]["status"] == "source_observed"
            and observed[field]["source_refs"],
            "MINUTE_RESEARCH_STATE_PROJECTION",
        )
    require(
        state["listed"] is True and state["suspended"] is False and state["is_st"] is False,
        "MINUTE_RESEARCH_ONLY_OBSERVED_ORDINARY_TRADABLE_STOCKS",
    )
    for field in ("reference_price", "upper_limit", "lower_limit"):
        require(
            field in state and state[field] is None and observed[field]["value"] is None,
            "MINUTE_RESEARCH_MUST_PRESERVE_UNKNOWN_LIMITS",
        )
    proof = state["tdx_previous_close"]
    require(
        proof["time"] == observed["previous_session"] + "T15:00:00+08:00"
        and number(proof["close"])
        == number(state["previous_close"])
        == number(observed["source_preclose"]["value"])
        and proof["manifest_sha256"],
        "MINUTE_RESEARCH_ACTUAL_PREVIOUS_CLOSE_MISMATCH",
    )


def interval(row):
    require(
        all(
            k in row and row[k] is None
            for k in ("bar_start", "bar_end", "available_at", "historical_available_at")
        ),
        "MINUTE_RESEARCH_MUST_PRESERVE_UNKNOWN_BAR_CLOCKS",
    )
    end = stamp(row["source_label"])
    require(
        stamp(row["replay_bar_end"]) == end
        and stamp(row["replay_bar_start"]) == end - timedelta(minutes=1),
        "MINUTE_RESEARCH_INTERVAL_MODEL_MISMATCH",
    )
    return end - timedelta(minutes=1), end


def validate_actions(doc):
    cov = doc["action_coverage"]
    require(
        cov["status"] == "bounded_sources_no_known_overlap"
        and cov["complete"] is False
        and cov["absence_proved"] is False
        and cov["evidence"],
        "MINUTE_RESEARCH_BOUNDED_ACTION_EVIDENCE_REQUIRED",
    )
    read = cov["public_action_query"]
    start, end = CALENDAR[0], DAYS[-1]
    require(
        read["milestones_checked"]
        == [
            "record_date",
            "ex_date",
            "payment_date",
            "share_credit_date",
            "new_share_trading_date",
        ],
        "MINUTE_RESEARCH_ACTION_MILESTONES_REQUIRED",
    )
    require(
        read["complete"] is False
        and read["absence_proved"] is False
        and read["review"]["start"] <= start <= end <= read["review"]["end"]
        and read["rows"] == []
        and doc["actions"] == [],
        "MINUTE_RESEARCH_KNOWN_ACTION_IN_WINDOW",
    )
    require(
        {(r["security"], r["report"]) for r in read["source_queries"]}
        == {(s, r) for s in IDS for r in ("RPT_SHAREBONUS_DET", "RPT_F10_DIVIDEND_MAIN")},
        "MINUTE_RESEARCH_ACTION_QUERY_COVERAGE",
    )
    for q in read["source_queries"]:
        require(
            q["source_query_complete"] is True
            and q["source_rows"] > 0
            and q["source_refs"]
            and q["coverage_start"] <= start <= end <= q["coverage_end"]
            and q["milestones"]
            and all(not v["in_range_dates"] for v in q["milestones"].values()),
            "MINUTE_RESEARCH_KNOWN_ACTION_OR_INCOMPLETE_QUERY",
        )
    for event in read["linked_events"]:
        for key in read["milestones_checked"]:
            require(
                not event.get(key) or not start <= event[key] <= end,
                "MINUTE_RESEARCH_KNOWN_ACTION_IN_WINDOW",
            )
    require(
        {e["security"] for e in cov["tdx_actions"]} == set(IDS),
        "MINUTE_RESEARCH_TDX_ACTION_COVERAGE",
    )
    for entry in cov["tdx_actions"]:
        rows, report = entry["rows"], entry["report"]
        require(
            len(rows) == report["returned_rows"] == report["wire_count"] > 0
            and report["security"] == entry["security"]
            and report["all_returned_record_dates_checked"] is True
            and report["full_corporate_action_absence_proved"] is False,
            "MINUTE_RESEARCH_TDX_ACTION_COVERAGE",
        )
        for r in rows:
            effective = date(r["year"], r["month"], r["day"]).isoformat()
            require(not start <= effective <= end, "MINUTE_RESEARCH_KNOWN_ACTION_IN_WINDOW")


def check_price(state, low, high):
    a, b = envelope(state["previous_close"])
    require(a < number(low) <= number(high) < b, "MINUTE_RESEARCH_PRICE_ENVELOPE_TOUCHED")
