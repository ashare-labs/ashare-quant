"""Strict boundary for AD's source-scoped, date-level corporate schedules.

This module neither retrieves observations nor infers an absent event. The full
owner response is retained in the frozen input, including lineage and coverage.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from ashare_quant.core.validation import digest, require

MODEL = {
    "schema": "aq.corporate-date-model.v1",
    "cash_availability": "before_open_on_payment_session",
    "share_credit": "before_open_on_credit_session",
    "share_tradability": "before_open_on_tradable_session",
    "fractional_shares": "reject_nonintegral_entitlement",
    "tax": "gross_cash_no_tax",
    "corrections": "immutable_run_reject_changed_source",
    "record_basis": "native_quantity_at_record_close",
}

POLICY = (
    "Source-scoped distributions with fixed record-close holdings; cash payment, "
    "share credit and tradability use their separate before-open session dates; "
    "gross cash without withholding; nonintegral share entitlements rejected."
)
STAGES = ("record", "cash", "stock_credit", "stock_tradable")


def number(value, field):
    try:
        require(isinstance(value, str), f"CORPORATE_DECIMAL_STRING_REQUIRED:{field}")
        result = Decimal(value)
        require(result.is_finite() and result >= 0, f"CORPORATE_INVALID_NUMBER:{field}")
        return result
    except InvalidOperation as exc:
        raise ValueError(f"CORPORATE_INVALID_NUMBER:{field}") from exc


def iso(value, field):
    require(isinstance(value, str), f"CORPORATE_DATE_REQUIRED:{field}")
    require(str(date.fromisoformat(value)) == value, f"CORPORATE_INVALID_DATE:{field}")
    return value


def schedule_rows(doc, ids, window, calendar):
    """Validate source evidence, then select without assuming account ownership.

    A selected payment/credit may have a record date before this run. Keep that
    source event; Inputs separately checks whether the starting account can own
    it. Only record-close quantities captured inside the run create entitlements.
    """
    schedule = doc.get("corporate_schedule")
    if schedule is None:
        return None
    require(not doc.get("actions"), "CORPORATE_LEGACY_ACTIONS_MUST_NOT_MIX")
    require(doc.get("corporate_account_model") == MODEL, "CORPORATE_DATE_MODEL_NOT_ACCEPTED")
    require(doc.get("research_model") != "price_only_no_actions", "CORPORATE_PRICE_ONLY_CONFLICT")
    require(schedule.get("schema") == "ad.aq-corporate-actions.v1", "CORPORATE_SCHEMA")
    require(schedule.get("event_identity_version") == 2, "CORPORATE_IDENTITY_VERSION")
    require(schedule.get("source_scoped_coverage_accepted") is True, "CORPORATE_SCOPE_NOT_ACCEPTED")
    require(schedule.get("tax_model") == "gross_cash_no_tax", "CORPORATE_TAX_MODEL_UNSUPPORTED")
    require(
        schedule.get("ready_for_declared_date_model") is True and schedule.get("issues") == [],
        "CORPORATE_SOURCE_ISSUES_BLOCK_EXECUTION",
    )
    for flag in (
        "historical_pit",
        "strategy_visible",
        "execution_permission",
        "economic_result_certified",
    ):
        require(schedule.get(flag) is False, f"CORPORATE_UNSUPPORTED_CERTIFICATION:{flag}")
    coverage = schedule["coverage"]
    pin = schedule.get("binding_sha256")
    require(
        isinstance(pin, str) and len(pin) == 64 and all(c in "0123456789abcdef" for c in pin),
        "CORPORATE_BINDING_PIN",
    )
    require(coverage.get("binding_sha256") == pin, "CORPORATE_COVERAGE_BINDING_MISMATCH")
    require(
        iso(coverage["start"], "coverage.start")
        <= window["start"]
        <= window["end"]
        <= iso(coverage["end"], "coverage.end"),
        "CORPORATE_WINDOW_NOT_COVERED",
    )
    by_security = {r["security"]: r for r in coverage["rows"]}
    require(len(by_security) == len(coverage["rows"]), "CORPORATE_DUPLICATE_COVERAGE")
    for sid in ids:
        row = by_security.get(sid, {})
        require(
            row.get("provider_tables_captured") is True
            and (
                row.get("economic_schedule_fields_complete") is True
                or (
                    row.get("events") == 0
                    and row.get("interval_state") == "empty_source_scoped_interval"
                )
            )
            and row.get("source_gaps") == []
            and row.get("event_field_gaps") == []
            and row.get("identity_boundaries") == []
            and row.get("interval_state") in {"events_observed", "empty_source_scoped_interval"},
            f"CORPORATE_COVERAGE_GAP:{sid}",
        )
    selected, seen, dates = [], {}, set()
    for sid in ids:
        require(
            len({r["entitlement_key"] for r in schedule["rows"] if r["security"] == sid})
            == by_security[sid].get("events"),
            "CORPORATE_COVERAGE_EVENT_COUNT",
        )
    for row in schedule["rows"]:
        sid, key = row["security"], row["entitlement_key"]
        require(row.get("type") == "distribution", "CORPORATE_EVENT_TYPE_UNSUPPORTED")
        require(
            isinstance(row.get("conflicts"), list) and row.get("identity_issues") == [],
            "CORPORATE_EVENT_CONFLICT",
        )
        for conflict in row["conflicts"]:
            require(isinstance(conflict, dict), "CORPORATE_EVENT_CONFLICT")
            field = conflict.get("field")
            lineage = row.get("field_lineage", {}).get(field, {})
            require(
                conflict.get("status") == "document_resolved_conflict"
                and lineage == {k: v for k, v in conflict.items() if k != "field"}
                and conflict.get("value") is not None
                and row.get(field) == conflict["value"]
                and conflict.get("resolution")
                and conflict.get("observations"),
                "CORPORATE_EVENT_CONFLICT",
            )
        require(key and row.get("event_id") == key, "CORPORATE_ENTITLEMENT_KEY")
        require(
            row.get("settlement_keys") == {s: key + ":" + s for s in STAGES},
            "CORPORATE_SETTLEMENT_KEYS",
        )
        require(
            row.get("revision_observation_ids")
            and len(set(row["revision_observation_ids"])) == len(row["revision_observation_ids"]),
            "CORPORATE_REVISION_EVIDENCE_REQUIRED",
        )
        if key in seen:
            require(seen[key] == digest(row), "CORPORATE_REVISION_RECONCILIATION_REQUIRED")
            continue  # Exact duplicates are one entitlement, including after recovery.
        seen[key] = digest(row)
        record, ex = iso(row["record_date"], "record_date"), iso(row["ex_date"], "ex_date")
        cash = number(row["cash_per_share"], "cash_per_share")
        stock = number(row["bonus_share_ratio"], "bonus_share_ratio") + number(
            row["capitalization_share_ratio"], "capitalization_share_ratio"
        )
        require(cash > 0 or stock > 0, "CORPORATE_EMPTY_DISTRIBUTION")
        require(record < ex, "CORPORATE_INVALID_CHRONOLOGY")
        active_dates = [record, ex]
        if cash:
            pay = iso(row["payment_date"], "payment_date")
            require(ex <= pay, "CORPORATE_INVALID_CHRONOLOGY")
            active_dates.append(pay)
        if stock:
            credit = iso(row["share_credit_date"], "share_credit_date")
            tradable = iso(row["new_shares_tradable_date"], "new_shares_tradable_date")
            require(ex <= credit <= tradable, "CORPORATE_INVALID_CHRONOLOGY")
            active_dates += [credit, tradable]
        if sid not in ids or record > window["end"] or max(active_dates) < window["start"]:
            continue
        for day in active_dates:
            if window["start"] <= day <= window["end"]:
                require(day in calendar, f"CORPORATE_STAGE_NOT_SESSION:{day}")
        # Only an in-run record can create an old-share cost allocation. Earlier
        # ex dates may be outside even the warmup calendar for delayed payments.
        if window["start"] <= record and ex <= window["end"]:
            require(
                calendar[calendar.index(ex) - 1] == record,
                "CORPORATE_NONADJACENT_RECORD_EX_UNSUPPORTED",
            )
        marker = (sid, record)
        require(
            marker not in dates,
            f"CORPORATE_SAME_RECORD_DISTRIBUTIONS_UNSUPPORTED:{sid}:{record}",
        )
        dates.add(marker)
        selected.append(row)
    return sorted(selected, key=lambda r: (r["record_date"], r["security"], r["entitlement_key"]))


def new_account_assumption(rows, start):
    """Describe the public cash-only task, using validated source events only."""
    return {
        "id": "corporate_initial_entitlements",
        "initial_account": "cash_only_no_positions_or_entitlements",
        "text": (
            "Each new task starts with cash only, no positions or inherited receivables. "
            "Warmup prices do not establish holdings. Events recorded before the run "
            "create no entitlement for this account; in-run record-close holdings "
            "determine subsequent cash and share entitlements."
        ),
        "prior_record_events": [
            {field: row.get(field) for field in (
                "security", "entitlement_key", "record_date", "ex_date", "payment_date",
                "share_credit_date", "new_shares_tradable_date",
            )}
            for row in rows if row["record_date"] < start
        ],
    }
