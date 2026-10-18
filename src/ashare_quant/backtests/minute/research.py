"""Provider-neutral admission for explicitly bounded closed-bar research.

Evidence identities prevent accidental substitution, not source authenticity or
historical PIT. Unknown exchange limits use the existing conservative execution
envelope; the native matcher/account retain all cash, T+1 and capacity checks.
"""

import hashlib
import json
import re
from datetime import date, timedelta

from ashare_quant.backtests.minute.bounded import check_price, envelope, interval
from ashare_quant.backtests.minute.inputs import number, require, stamp

SCHEMA = "ad-minute-research.v1"
PROFILE = "source-labelled-closed-bars.v1"
MILESTONES = [
    "record_date",
    "ex_date",
    "payment_date",
    "share_credit_date",
    "new_share_trading_date",
]
ASSUMPTIONS = [
    "source_label_is_model_interval_end_not_verified_exchange_interval",
    "bar_visible_at_model_interval_end_not_observed_historical_availability",
    "dated_source_states_replayed_at_0900_not_observed_historical_availability",
    "daily_status_and_evidenced_activity_not_proof_of_no_intraday_halt",
    "bounded_action_sources_have_no_omitted_action_affecting_new_positions",
    "no_ex_right_reference_change_within_the_selected_no_known_action_window",
    "source_precision_and_declared_derivations_preserved_without_repair",
]


def digest(value):
    """AD source contract canonical JSON (UTF-8, no non-finite values)."""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def refs(doc, values):
    catalog = doc["source_refs"]["evidence"]
    require(
        isinstance(values, list)
        and values
        and all(isinstance(v, str) and v in catalog for v in values),
        "MINUTE_RESEARCH_EVIDENCE_REFERENCE",
    )


def validate_document(doc):
    require(
        doc.get("content_sha256")
        == digest({k: v for k, v in doc.items() if k != "content_sha256"}),
        "MINUTE_RESEARCH_CONTENT_IDENTITY",
    )
    model = doc["research"]
    require(
        model["profile"] == PROFILE
        and model["purpose"] == "retrospective_research"
        and model["assumptions"] == ASSUMPTIONS
        and model["strict_execution_admitted"] is False
        and model["interval_verified"] is False
        and model["price_limit_verified"] is False
        and model["historical_pit"] is False
        and model["envelope"] == "raw-previous-close-4pct-inward-cent.v1"
        and model["capabilities"]
        == {
            "matching": "next_open",
            "lifecycle": "closed_bars",
            "capacity_policy": "strict_minute",
            "initial_positions": "flat",
            "terminal_positions": "flat",
        },
        "MINUTE_RESEARCH_PROFILE_REQUIRED",
    )
    require(
        doc["visibility"] == "assumed_closed"
        and doc["state_visibility"] == "posthoc_daily_replay"
        and doc["limitations"],
        "MINUTE_RESEARCH_SEPARATE_REPLAY_CLOCKS_REQUIRED",
    )
    catalog = doc["source_refs"]["evidence"]
    require(
        isinstance(catalog, dict)
        and catalog
        and all(
            isinstance(v, dict)
            and v.get("source")
            and re.fullmatch(r"[0-9a-f]{64}", v.get("sha256", ""))
            for v in catalog.values()
        ),
        "MINUTE_RESEARCH_EVIDENCE_CATALOG",
    )
    refs(doc, doc["state_replay"]["evidence"])
    lineage = doc["source_refs"]["ad_lineage"]
    require(
        re.fullmatch(r"[0-9a-f]{64}", lineage["snapshot_id"])
        and lineage["snapshot_id"] == doc["producer"]["snapshot_id"]
        and lineage["batches"]
        and len({b["source"]["id"] for b in lineage["batches"]}) == len(lineage["batches"]),
        "MINUTE_RESEARCH_SOURCE_COVERAGE",
    )
    calendars = model["calendars"]
    for exchange in {sid.rsplit(".", 1)[-1] for sid in doc["securities"]}:
        cal = calendars[exchange]
        refs(doc, cal["source_refs"])
        a, b = date.fromisoformat(doc["calendar"][0]), date.fromisoformat(doc["calendar"][-1])
        require(0 < (b - a).days <= 366, "MINUTE_RESEARCH_CALENDAR_RANGE")
        expected = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        require(
            set(cal["days"]) == set(expected)
            and all(type(v) is bool for v in cal["days"].values())
            and [d for d in expected if cal["days"][d]] == doc["calendar"],
            "MINUTE_RESEARCH_CALENDAR_EVIDENCE",
        )


def validate_state(state):
    observed = state["source_observation"]
    require(
        observed["security"] == state["security"]
        and observed["date"] == state["date"]
        and observed["historical_available_at"] is None
        and observed["intraday_halts_verified"] is False
        and observed["dependency_conflicts"] == []
        and observed["source_quality"]["execution_permission"] is False
        and observed["source_quality"]["limitations"],
        "MINUTE_RESEARCH_STATE_IDENTITY",
    )
    for field in ("listed", "suspended", "is_st", "previous_close", "first_listing_date"):
        fact = observed[field]
        require(
            fact["value"] == state[field]
            and fact["status"] == "source_observed"
            and fact["source_refs"],
            "MINUTE_RESEARCH_STATE_PROJECTION",
        )
    require(
        state["listed"] is True and state["suspended"] is False and state["is_st"] is False,
        "MINUTE_RESEARCH_ONLY_OBSERVED_ORDINARY_TRADABLE_STOCKS",
    )
    require(
        date.fromisoformat(state["first_listing_date"])
        <= date.fromisoformat(state["date"]) - timedelta(days=30),
        "MINUTE_RESEARCH_RECENT_LISTING_UNSUPPORTED",
    )
    for field in ("reference_price", "upper_limit", "lower_limit"):
        require(
            field in state and state[field] is None and observed[field]["value"] is None,
            "MINUTE_RESEARCH_MUST_PRESERVE_UNKNOWN_LIMITS",
        )


def validate_state_binding(state, doc, previous):
    refs(doc, state["evidence"]["source_refs"])
    observed = state["source_observation"]
    for field in ("listed", "suspended", "is_st", "previous_close", "first_listing_date"):
        refs(doc, observed[field]["source_refs"])
    proof = state["previous_close_observation"]
    refs(doc, proof["source_refs"])
    require(
        proof["security"] == state["security"]
        and proof["date"] == previous
        and proof["price_basis"] == "raw"
        and number(proof["close"]) == number(state["previous_close"])
        and observed["previous_session"] == previous,
        "MINUTE_RESEARCH_ACTUAL_PREVIOUS_CLOSE_MISMATCH",
    )


def validate_bar_binding(row, doc):
    proof, raw = row["evidence"], row["evidence"]["ad_row"]
    sources = {b["source"]["id"]: b["source"] for b in doc["source_refs"]["ad_lineage"]["batches"]}
    source = sources.get(proof["source_id"], {})
    require(
        proof["snapshot_id"] == doc["producer"]["snapshot_id"]
        and proof["source_contract_sha256"] == digest(source)
        and proof["row_sha256"] == digest(raw)
        and raw["source_id"] == proof["source_id"]
        and source.get("label") == "end"
        and source.get("kind") in {"local_observation", "local_reconstruction", "synthetic"}
        and source.get("price_basis") == "unadjusted"
        and (source.get("kind") != "synthetic") == doc["real_market_data"]
        and raw["symbol"] == row["security"]
        and raw["source_label"] == row["source_label"]
        and raw["available_at"] is None
        and raw["quality"] == row["quality"]
        and raw["quality"]
        in ({"observed", "inferred", "unverified"} if doc["real_market_data"] else {"synthetic"})
        and "source_fields" in raw
        and all(
            number(raw[k], zero=k in {"volume", "amount"})
            == number(row[k], zero=k in {"volume", "amount"})
            for k in ("open", "high", "low", "close", "volume", "amount")
        ),
        "MINUTE_RESEARCH_BAR_SOURCE_BINDING",
    )
    if "phase_evidence" in row:
        refs(doc, row["phase_evidence"]["source_refs"])


def validate_activity(row, volume, amount, *, full):
    if volume > 0:
        require(amount > 0, "MINUTE_INVALID_NUMBER")
        return
    require(amount == 0, "MINUTE_RESEARCH_ZERO_VOLUME_AMOUNT_CONFLICT")
    phase = row.get("phase_evidence", {})
    require(
        row.get("session_phase") == "closing_auction"
        and "14:57" < stamp(row["source_label"]).strftime("%H:%M") <= "15:00"
        and phase.get("model") == "source-labelled-closing-stage.v1"
        and phase.get("source_label") == row["source_label"]
        and phase.get("source_row_sha256") == row["evidence"]["row_sha256"]
        and phase.get("historical_phase_verified") is False
        and phase.get("source_refs"),
        "MINUTE_RESEARCH_ZERO_VOLUME_PHASE_EVIDENCE",
    )


def validate_actions(doc):
    cov = doc["action_coverage"]
    start = doc["calendar"][doc["calendar"].index(doc["days"][0]) - 1]
    end = doc["days"][-1]
    require(
        cov["status"] == "bounded_sources_no_known_overlap"
        and cov["complete"] is False
        and cov["absence_proved"] is False
        and cov["historical_pit"] is False
        and cov["start"] <= start <= end <= cov["end"],
        "MINUTE_RESEARCH_BOUNDED_ACTION_EVIDENCE_REQUIRED",
    )
    refs(doc, cov["evidence"])
    require(
        {q["security"] for q in cov["queries"]} == set(doc["securities"]),
        "MINUTE_RESEARCH_ACTION_QUERY_COVERAGE",
    )
    for q in cov["queries"]:
        refs(doc, q["source_refs"])
        require(
            q["query_complete"] is True
            and q["start"] <= start <= end <= q["end"]
            and q["milestones_checked"] == MILESTONES
            and type(q["source_rows"]) is int
            and q["source_rows"] == len(q["events"])
            and q["unknown_dates_are_not_no_event"] is True,
            "MINUTE_RESEARCH_INCOMPLETE_ACTION_QUERY",
        )
        for event in q["events"]:
            require(event["security"] == q["security"], "MINUTE_RESEARCH_ACTION_IDENTITY")
            refs(doc, event["source_refs"])
            for field in MILESTONES:
                require(field in event, "MINUTE_RESEARCH_ACTION_MILESTONES_REQUIRED")
                value = event[field]
                require(
                    value is None
                    or (
                        date.fromisoformat(value).isoformat() == value and not start <= value <= end
                    ),
                    "MINUTE_RESEARCH_KNOWN_ACTION_IN_WINDOW",
                )


def validate_execution(settings):
    require(
        settings["matching"] == "next_open"
        and settings["lifecycle"] == "closed_bars"
        and settings["capacity_policy"] == "strict_minute",
        "MINUTE_RESEARCH_EXECUTION_CAPABILITY_UNSUPPORTED",
    )


__all__ = ["check_price", "envelope", "interval"]
