"""Provider-neutral admission for explicitly bounded closed-bar research.

Evidence identities prevent accidental substitution, not source authenticity or
historical PIT. Unknown exchange limits use the existing conservative execution
envelope; the native matcher/account retain all cash, T+1 and capacity checks.
"""

import hashlib
import json
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ashare_quant.backtests.minute.bounded import check_price as bounded_check_price
from ashare_quant.backtests.minute.bounded import envelope, interval
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
CORPORATE_PROFILE = "source-labelled-corporate-date-model.v1"
LIMIT_MODEL = "daily-preclose-mainboard-5pct-10pct.v1"
CORPORATE_ASSUMPTIONS = [
    *ASSUMPTIONS[:4],
    "source_scoped_cash_distribution_dates_and_gross_entitlements_not_all_action_types",
    "daily_source_preclose_5pct_ST_10pct_mainboard_limits_are_models_not_exchange_certification",
    ASSUMPTIONS[-1],
]


def is_corporate(doc):
    return doc["research"]["profile"] == CORPORATE_PROFILE


def daily_model_bounds(state):
    raw = state["daily_limit_model"]["source_row"]
    reference = number(raw["preclose"])
    rate = Decimal("0.05") if state["is_st"] else Decimal("0.10")
    with localcontext(prec=50):
        return tuple(
            (reference * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for multiplier in (1 - rate, 1 + rate)
        )


def check_price(state, low, high):
    if "daily_limit_model" not in state:
        return bounded_check_price(state, low, high)
    lower, upper = daily_model_bounds(state)
    # Preserve native binary64 observations; half a cent is an explicit
    # consistency tolerance, not a rewritten quote or a relaxed order limit.
    require(
        number(low) >= lower - Decimal("0.005") and number(high) <= upper + Decimal("0.005"),
        "MINUTE_DAILY_MODEL_PRICE_CONFLICT",
    )


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
    corporate = is_corporate(doc)
    require(
        model["profile"] in {PROFILE, CORPORATE_PROFILE}
        and model["purpose"] == "retrospective_research"
        and model["assumptions"] == (CORPORATE_ASSUMPTIONS if corporate else ASSUMPTIONS)
        and model["strict_execution_admitted"] is False
        and model["interval_verified"] is False
        and model["price_limit_verified"] is False
        and model["historical_pit"] is False
        and model["envelope"]
        == (LIMIT_MODEL if corporate else "raw-previous-close-4pct-inward-cent.v1")
        and model["capabilities"]
        == {
            "matching": "next_open",
            "lifecycle": "closed_bars",
            "capacity_policy": "strict_minute",
            "initial_positions": "flat",
            "terminal_positions": "flat",
            **({"corporate_actions": "source_scoped_cash_dates"} if corporate else {}),
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
        require(0 < (b - a).days <= (740 if corporate else 366), "MINUTE_RESEARCH_CALENDAR_RANGE")
        expected = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        require(
            set(cal["days"]) == set(expected)
            and all(type(v) is bool for v in cal["days"].values())
            and [d for d in expected if cal["days"][d]] == doc["calendar"],
            "MINUTE_RESEARCH_CALENDAR_EVIDENCE",
        )


def validate_state(state, *, corporate=False):
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
    require(("daily_limit_model" in state) == corporate, "MINUTE_LIMIT_MODEL_PROFILE")
    if corporate:
        model = state["daily_limit_model"]
        raw = model["source_row"]
        require(
            model["schema"] == LIMIT_MODEL
            and model["source_row_sha256"] == digest(raw)
            and raw["date"] == state["date"]
            and raw["symbol"]
            == ("sh" if state["security"].endswith("XSHG") else "sz") + state["security"][:6]
            and raw["tradestatus"] == ("0" if state["suspended"] else "1")
            and raw["is_st"] == ("1" if state["is_st"] else "0")
            and raw.get("source")
            and raw.get("observed_at")
            and model["exchange_limits_verified"] is False,
            "MINUTE_DAILY_MODEL_SOURCE_BINDING",
        )
        number(raw["preclose"])


def validate_state_binding(state, doc, previous):
    refs(doc, state["evidence"]["source_refs"])
    observed = state["source_observation"]
    if is_corporate(doc):
        refs(doc, state["daily_limit_model"]["source_refs"])
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
    # Recompute from the immutable AD row: accept_research cannot override a
    # source denial, including in legacy exports lacking a top-level policy.
    policy = source_use_policy(raw, source)
    require(row.get("use_policy", policy) == policy, "MINUTE_RESEARCH_USE_POLICY_BINDING")
    return policy


def source_use_policy(raw, source):
    """Consumer-side validation, including old exports with nested native facts."""
    decision = {"execution": "not_granted", "fresh_valuation": "not_granted", "reasons": []}
    obs = raw["source_fields"].get("native_observation")
    require(
        obs is not None or "audit_manifest_sha256" not in source,
        "MINUTE_RESEARCH_USE_SOURCE_BINDING",
    )
    if obs is not None:
        require(
            obs["security"] == raw["symbol"]
            and obs["source_label"] == raw["source_label"]
            and obs["source_file_sha256"] == source.get("source_sha256")
            and type(obs["source_row"]) is int
            and obs["source_row"] >= 0
            and isinstance(obs["source_path"], str)
            and bool(obs["source_path"])
            and obs["price_status"] in {"invalid", "conflict", "unverified"}
            and all(
                number(obs[k], zero=k in {"volume", "amount"})
                == number(raw[k], zero=k in {"volume", "amount"})
                for k in ("open", "high", "low", "close", "volume", "amount")
            ),
            "MINUTE_RESEARCH_USE_SOURCE_BINDING",
        )
        reasons = sorted(
            set(obs.get("diagnostics", []))
            & {
                "nonfinite_volume",
                "negative_volume",
                "noninteger_volume",
                "nonfinite_amount",
                "negative_amount",
                "null_volume",
                "null_amount",
            }
        )
        denied = obs["price_status"] in {"invalid", "conflict"}
        if denied:
            reasons.append("price_" + obs["price_status"])
        decision = {
            "execution": "deny" if denied or reasons else "not_granted",
            "fresh_valuation": "deny" if denied else "not_granted",
            "reasons": reasons,
        }
        require(obs.get("use_decision", decision) == decision, "MINUTE_RESEARCH_USE_POLICY_BINDING")
    return {
        "schema": "minute-row-use.v1",
        **decision,
        "binding": {
            "source_id": raw["source_id"],
            "source_contract_sha256": digest(source),
            "row_sha256": digest(raw),
        },
    }


def validate_gaps(doc, expected, present):
    policy = doc.get("gap_policy", "reject")
    require(policy in {"reject", "explicit_unavailable"}, "MINUTE_RESEARCH_GAP_POLICY")
    declared = doc.get("unavailable_minutes", [])
    require(policy == "explicit_unavailable" or not declared, "MINUTE_RESEARCH_GAP_POLICY")
    gaps = {}
    for gap in declared:
        key = gap["security"], stamp(gap["source_label"])
        proof = gap["evidence"]
        coverage = proof["coverage"]
        known = [
            r
            for r in coverage.get("unavailable_quotes", [])
            if r["security"] == key[0] and stamp(r["source_label"]) == key[1]
        ]
        require(len(known) <= 1, "MINUTE_RESEARCH_GAP_BINDING")
        reason = "no_quote_in_selected_snapshot"
        if known:
            quote = known[0]
            sources = {
                b["source"]["id"]: b["source"] for b in doc["source_refs"]["ad_lineage"]["batches"]
            }
            require(
                quote.get("price_status") == "invalid"
                and quote.get("missing_reason") == "confirmed_invalid_price"
                and set(quote.get("missing_fields", []))
                == {"open", "high", "low", "close", "volume", "amount"}
                and all(quote.get(k) is None for k in quote["missing_fields"])
                and quote.get("use_decision", {}).get("execution") == "deny"
                and quote.get("use_decision", {}).get("fresh_valuation") == "deny"
                and quote["source_contract_sha256"] == digest(sources[quote["source_id"]]),
                "MINUTE_RESEARCH_GAP_BINDING",
            )
            reason = quote["missing_reason"]
        require(
            key not in gaps
            and key in expected
            and key not in present
            and gap["reason"] == reason
            and gap["execution"] == gap["fresh_valuation"] == "deny"
            and proof["snapshot_id"] == coverage["snapshot_id"] == doc["producer"]["snapshot_id"]
            and proof["coverage_sha256"] == digest(coverage)
            and not any(
                coverage[k]
                for k in ("unknown_calendar_dates", "unexpected", "excluded", "duplicate_keys")
            )
            and {"symbol": key[0], "bar_start": (key[1] - timedelta(minutes=1)).isoformat()}
            in coverage["missing"],
            "MINUTE_RESEARCH_GAP_BINDING",
        )
        gaps[key] = gap
    require(present | set(gaps) == expected, "MINUTE_BAR_COVERAGE_GAP")
    return gaps


def validate_activity(row, volume, amount, *, full, corporate=False):
    if volume > 0:
        require(amount > 0, "MINUTE_INVALID_NUMBER")
        return
    require(amount == 0, "MINUTE_RESEARCH_ZERO_VOLUME_AMOUNT_CONFLICT")
    if corporate:
        # This profile retains source-bound zero activity at any session label.
        # Native inactive/volume limits cancel a market attempt there. The price
        # remains only an uncertified research observation; no market phase,
        # suspension or availability fact is inferred from a zero quantity.
        return
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
    if is_corporate(doc):
        from ashare_quant.portfolio.corporate import schedule_rows

        cov = doc["action_coverage"]
        schedule = doc["corporate_schedule"]
        require(
            cov["status"] == "source_scoped_corporate_date_model"
            and cov["complete"] is False
            and cov["absence_proved"] is False
            and cov["historical_pit"] is False
            and cov["binding_sha256"] == schedule["binding_sha256"]
            and cov["response_sha256"] == digest(schedule),
            "MINUTE_CORPORATE_COVERAGE_BINDING",
        )
        refs(doc, cov["evidence"])
        rows = schedule_rows(
            doc,
            doc["securities"],
            {"start": doc["days"][0], "end": doc["days"][-1]},
            doc["calendar"],
        )
        require(
            rows is not None
            and all(
                Decimal(r["bonus_share_ratio"]) == Decimal(r["capitalization_share_ratio"]) == 0
                for r in rows
            ),
            "MINUTE_CORPORATE_ONLY_CASH_DISTRIBUTIONS",
        )
        return rows
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
