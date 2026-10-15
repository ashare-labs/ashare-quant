"""Validate an immutable AD export. No network, collection, inferred facts or store."""

import hashlib
import json
from datetime import date
from decimal import Decimal, DecimalException
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


class InputNumberError(ValueError):
    """A numeric refusal with an exact source location, also usable by HTTP preflight."""

    def __init__(self, code, security, day, field, value):
        super().__init__(f"{code}:{security}:{day}:{field}")
        self.gap = {
            "code": code,
            "security": security,
            "date": day,
            "field": field,
            "detail": "Expected a finite positive number (volume may be zero)",
            "value_preview": str(value)[:160],
        }


def source_number(value, security, day, field, code="INVALID_DAILY_NUMBER"):
    try:
        number = Decimal(str(value))
        if number.is_finite() and (number >= 0 if field == "volume" else number > 0):
            return number
    except (DecimalException, ValueError, TypeError):
        pass
    raise InputNumberError(code, security, day, field, value)


class Inputs:
    def __init__(self, path, manifest, window, *, accept_price_only_research=False):
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.document = doc = json.loads(raw)
        self.no_price_limits = doc.get("price_limit_policy") == "disabled_for_price_only_diagnostic"
        if self.no_price_limits:
            require(
                accept_price_only_research and doc.get("research_model") == "price_only_no_actions",
                "PRICE_LIMIT_OPT_OUT_REQUIRES_PRICE_ONLY_DIAGNOSTIC",
            )
        require(doc["schema"] == "ad-daily-portfolio.v1", "OWNER_EXPORT_SCHEMA")
        require(
            doc["producer"]["name"] == "ashare-data" or doc["real_market_data"] is False,
            "ONLY_AD_REAL_SOURCE_EXPORT_ALLOWED",
        )
        require(type(doc["real_market_data"]) is bool, "REAL_SOURCE_FLAG_REQUIRED")
        require(doc.get("source_refs") and "limitations" in doc, "SOURCE_EVIDENCE_REQUIRED")
        require(
            doc["price_basis"] == "raw" and doc["volume_unit"] == "share",
            "RAW_PRICE_SHARE_VOLUME_REQUIRED",
        )
        self.ids = tuple(manifest["securities"])
        require(len(set(self.ids)) == len(self.ids), "DUPLICATE_SECURITY")
        require(set(doc["securities"]) == set(self.ids), "FROZEN_UNIVERSE_MISMATCH")
        self.calendar = doc["calendar"]
        require(self.calendar == sorted(set(self.calendar)), "CALENDAR_ORDER_OR_DUPLICATES")
        for day in self.calendar:
            require(str(date.fromisoformat(day)) == day, "CALENDAR_DATE_FORMAT")
        require(
            window["start"] in self.calendar and window["end"] in self.calendar,
            "WINDOW_ENDPOINT_NOT_REAL_SESSION_REQUIRES_EXPLICIT_CONTRACT_CORRECTION",
        )
        start, end = self.calendar.index(window["start"]), self.calendar.index(window["end"])
        require(
            start >= 120 and end >= start and end + 1 < len(self.calendar),
            "WARMUP_120_OR_SUCCESSOR_MISSING",
        )
        self.days = self.calendar[start : end + 1]
        self.previous = {d: self.calendar[self.calendar.index(d) - 1] for d in self.days}
        self.rows = {}
        for row in doc["daily"]:
            key = (row["security"], row["date"])
            require(key not in self.rows, f"DUPLICATE_DAILY_ROW:{key}")
            self.rows[key] = row
        self.gaps = list(doc.get("additional_gaps", []))
        for sid in self.ids:
            for day in self.calendar[start - 120 : end + 1]:
                row = self.rows.get((sid, day))
                if row is None:
                    self.gaps.append({"security": sid, "date": day, "field": "daily"})
                    continue
                for field in (
                    ("close",)
                    if day < window["start"]
                    else ("open", "high", "low", "close", "volume")
                ):
                    value = row.get(field)
                    if value is None:
                        self.gaps.append({"security": sid, "date": day, "field": field})
                    else:
                        source_number(value, sid, day, field)
                if day >= window["start"]:
                    if all(row.get(f) is not None for f in ("open", "high", "low", "close")):
                        op, hi, lo, cl = (
                            Decimal(str(row[f])) for f in ("open", "high", "low", "close")
                        )
                        require(
                            lo <= min(op, cl) <= max(op, cl) <= hi,
                            f"INCONSISTENT_OHLC:{sid}:{day}",
                        )
                    for field in (
                        ("suspended",)
                        if self.no_price_limits
                        else ("suspended", "upper_limit", "lower_limit")
                    ):
                        if row.get(field) is None:
                            self.gaps.append({"security": sid, "date": day, "field": field})
                    if row.get("suspended") is not None:
                        require(type(row["suspended"]) is bool, "SUSPENSION_MUST_BE_BOOLEAN")
                    for field in ("upper_limit", "lower_limit"):
                        if row.get(field) is not None:
                            source_number(row[field], sid, day, field, "INVALID_LIMIT_PRICE")
                    if all(row.get(f) is not None for f in ("upper_limit", "lower_limit")):
                        require(
                            Decimal(str(row["lower_limit"])) <= Decimal(str(row["upper_limit"])),
                            f"INVERTED_PRICE_LIMITS:{sid}:{day}",
                        )
        self.actions = doc.get("actions", [])
        seen_actions = set()
        for action in self.actions:
            key = (action["security"], action["kind"], action["ex_date"])
            require(key not in seen_actions, "DUPLICATE_OR_OVERLAPPING_ACTION")
            seen_actions.add(key)
            require(
                action["security"] in self.ids and action["kind"] in {"cash_dividend", "split"},
                "UNSUPPORTED_ACTION_KIND_OR_SECURITY",
            )
            require(action["ex_date"] in self.calendar, "ACTION_EX_DATE_OUTSIDE_CALENDAR")
            if action["kind"] == "cash_dividend":
                i = self.calendar.index(action["ex_date"])
                require(
                    i > 0 and action["record_date"] == self.calendar[i - 1],
                    "NON_ADJACENT_DIVIDEND_RECORD_EX_DATE_UNSUPPORTED",
                )
                require(action["pay_date"] >= action["ex_date"], "DIVIDEND_PAY_BEFORE_EX")
                source_number(
                    action["cash_per_share"],
                    action["security"],
                    action["ex_date"],
                    "cash_per_share",
                    "INVALID_DIVIDEND_VALUE",
                )
            else:
                require(
                    action["credit_date"] == action["tradable_date"] == action["ex_date"],
                    "SEPARATE_SHARE_CREDIT_TRADABILITY_UNSUPPORTED",
                )
                source_number(
                    action["ratio"],
                    action["security"],
                    action["ex_date"],
                    "ratio",
                    "INVALID_SPLIT_RATIO",
                )
        coverage = doc.get("action_coverage", {})
        from .corporate import schedule_rows

        self.corporate_actions = schedule_rows(doc, self.ids, window, self.calendar)
        initial = manifest.get("execution", {}).get("initial_positions", {})
        if initial:
            require(
                self.corporate_actions is not None
                and manifest["execution"].get("initial_position_model")
                == "research_seed_previous_close"
                and isinstance(initial, dict)
                and set(initial) <= set(self.ids)
                and all(type(q) is int and 0 < q <= 2**53 for q in initial.values()),
                "CORPORATE_INITIAL_HOLDING_MODEL_REQUIRED",
            )
            for sid in initial:
                prior = self.rows.get((sid, self.previous[window["start"]]), {})
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "upper_limit",
                    "lower_limit",
                ):
                    source_number(prior.get(field), sid, self.previous[window["start"]], field)
        self.price_only = doc.get("research_model") == "price_only_no_actions"
        if self.price_only:
            require(
                coverage.get("status") == "unavailable"
                and not self.actions
                and doc.get("economic_acceptance")
                == "price_only_diagnostic_not_economic_acceptance"
                and doc.get("state_mode") == "retrospective_model",
                "PRICE_ONLY_SCOPE_MUST_EXCLUDE_ECONOMIC_ACCEPTANCE",
            )
            if not accept_price_only_research:
                self.gaps.append(
                    {
                        "field": "price_only_opt_in",
                        "detail": "Explicit --accept-price-only-research required",
                    }
                )
        if (
            coverage.get("status") not in {"source_covered", "synthetic_known"}
            and not self.price_only
        ):
            self.gaps.append({"field": "corporate_actions", "detail": coverage})
        require(
            not doc["real_market_data"] or coverage.get("status") != "synthetic_known",
            "REAL_INPUT_CANNOT_USE_SYNTHETIC_ACTION_COVERAGE",
        )
        # Listing state is a qualified bounded model unless the producer supplies dates.
        require(
            doc.get("listing_coverage")
            in {"source_covered", "scoped_research_model", "synthetic_known"},
            "LISTING_COVERAGE_REQUIRED",
        )
        require(
            not doc["real_market_data"] or doc["listing_coverage"] != "synthetic_known",
            "REAL_INPUT_CANNOT_USE_SYNTHETIC_LISTING_COVERAGE",
        )
        self.window = window
        if doc["real_market_data"]:
            require(
                doc.get("owner_api_reads") and doc.get("owner_history_sha256"),
                "PUBLIC_OWNER_API_READ_EVIDENCE_REQUIRED",
            )
            require(set(doc["owner_history_sha256"]) == set(self.days), "OWNER_HISTORY_COVERAGE")
            if not any(g.get("field") in {"daily", "close"} for g in self.gaps):
                for day in self.days:
                    require(
                        digest(self.history(day)) == doc["owner_history_sha256"][day],
                        f"OWNER_HISTORY_RECONCILIATION:{day}",
                    )

    def preflight(self):
        return {
            "schema": "aq-daily-preflight.v1",
            "input_sha256": self.sha256,
            "window": self.window,
            "sessions": len(self.days),
            "securities": list(self.ids),
            "ready": not self.gaps,
            "gaps": self.gaps,
            "real_market_data": self.document["real_market_data"],
            "limitations": self.document["limitations"],
            "historical_pit_verified": False,
            "state_mode": self.document.get("state_mode", "synthetic_known"),
            "assumptions": self.document.get("assumptions", []),
            "economic_acceptance": self.document.get("economic_acceptance", "synthetic_only"),
            **(
                {"corporate_account_model": self.document["corporate_account_model"]}
                if self.corporate_actions is not None
                else {}
            ),
            **(
                {"price_limit_policy": self.document["price_limit_policy"]}
                if self.no_price_limits
                else {}
            ),
            "action_coverage": self.document["action_coverage"],
            "research_model": self.document.get("research_model", "corporate_action_accounting"),
        }

    def row(self, sid, day):
        require(sid in self.ids and (sid, day) in self.rows, f"OWNER_ROW_OUT_OF_SCOPE:{sid}:{day}")
        return self.rows[sid, day]

    def history(self, day):
        index = self.calendar.index(day)
        require(day in self.days and index >= 120, "SIGNAL_DATE_OUT_OF_SCOPE")
        days = self.calendar[index - 120 : index]
        return {sid: tuple(self.row(sid, d)["close"] for d in days) for sid in self.ids}
