"""Read-only supplement review. These observations are never engine inputs."""

import copy
import json
from decimal import Decimal
from pathlib import Path

from .compare import clock, sha

MARK_FIELDS = {
    "/positions/0/last_price",
    "/positions/0/market_value",
    "/account/positions_value",
    "/account/total_value",
}


def _require(condition, code):
    if not condition:
        raise ValueError("PREMARKET_EVIDENCE_" + code)


def _one(events, kind, label):
    rows = [r for r in events if r["kind"] == kind and r.get("label") == label]
    _require(len(rows) == 1, "AMBIGUOUS_CHECKPOINT")
    return rows[0]


def _number(field):
    value = Decimal(str(field["value"]))
    _require(value.is_finite(), "NONFINITE")
    _require(float.fromhex(field["float_hex"]) == float(value), "NATIVE_PRECISION")
    _require(float(field["float17"]) == float(value), "NATIVE_PRECISION")
    return value


def checkpoint(events, label, *, at, previous_date, security):
    """Require contemporaneous public reads of prior data and unchanged accounts."""
    rows = [r for r in events if clock(r["time"]) == at]
    before = _one(rows, "account_precision", label + ":before_reads")
    after = _one(rows, "account_precision", label + ":after_reads")
    _require(before["portfolio"] == after["portfolio"], "READ_MUTATED_ACCOUNT")
    _require(before["positions"] == after["positions"], "READ_MUTATED_ACCOUNT")
    _require(len(before["positions"]) == 1, "POSITION_SCOPE")
    position = before["positions"][0]
    _require(position["security"] == security, "SECURITY")
    values = {k: _number(v) for k, v in position["fields"].items()}
    current = _one(rows, "current_precision", label)
    _require(current["symbol"] == security, "SECURITY")
    mark = _number(current["fields"]["last_price"])
    _require(mark > 0 and mark == values["price"], "MARK_INCONSISTENT")
    _require(values["total_amount"] > 0, "QUANTITY")
    _require(mark * values["total_amount"] == values["value"], "VALUATION")
    portfolio = {k: _number(v) for k, v in before["portfolio"].items()}
    _require(portfolio["positions_value"] == values["value"], "VALUATION")
    _require(
        portfolio["available_cash"] + values["value"] == portfolio["total_value"],
        "VALUATION",
    )
    probe = _one(rows, "probe_clock", label)
    _require(probe["previous_date"] == previous_date, "PREVIOUS_DATE")
    closes, evidence_seqs = {}, [before["seq"], current["seq"], after["seq"]]
    for period, frequency in (("daily", "daily"), ("minute", "1m")):
        for fq in (None, "pre"):
            key = f"{label}:prior_{period}:{fq}"
            request = _one(rows, "data_request", key)
            result = _one(rows, "precise_data_result", key)
            params = request["parameters"]
            end = previous_date + (" 15:00:00" if period == "minute" else "")
            _require(
                request["symbol"] == security
                and params["end_date"] == end
                and params["frequency"] == frequency
                and params["fq"] == fq
                and request["seq"] < result["seq"],
                "QUERY_SCOPE",
            )
            _require(
                result["rows"]
                and all(r["index"][:10] <= previous_date for r in result["rows"])
                and result["rows"][-1]["index"][:10] == previous_date,
                "FUTURE_OR_MISSING_ROW",
            )
            closes[f"{period}_{fq}"] = _number(result["rows"][-1]["fields"]["close"])
            evidence_seqs.extend([request["seq"], result["seq"]])
    _require(closes["daily_pre"] == closes["minute_pre"] == mark, "MARK_INCONSISTENT")
    _require(closes["daily_None"] == closes["minute_None"], "RAW_CLOSE_INCONSISTENT")
    return dict(
        at=at,
        previous_date=previous_date,
        security=security,
        public_mark=str(mark),
        raw_previous_close=str(closes["daily_None"]),
        position={k: str(v) for k, v in values.items()},
        portfolio={k: str(v) for k, v in portfolio.items()},
        event_seqs=sorted(evidence_seqs),
        reads_leave_account_unchanged=True,
    )


def read_supplement(path, expected_sha256, original_settings):
    """Verify a frozen observation file without admitting it as a market feed."""
    _require(sha(path) == expected_sha256, "HASH")
    events = [json.loads(line) for line in Path(path).read_text().splitlines()]
    _require([r["seq"] for r in events] == list(range(1, len(events) + 1)), "SEQUENCE")
    _require(events[-1]["event_count_including_end"] == len(events), "SEQUENCE")
    _require(not any(r["kind"] == "observation_error" for r in events), "OBSERVATION_ERROR")
    identity = events[0]
    _require(identity["kind"] == "identity", "IDENTITY")
    for key in (
        "symbol", "start_date", "end_date", "frequency", "initial_cash", "use_real_price",
        "avoid_future_data", "order_volume_ratio", "all_fees", "slippage", "probe_times",
    ):
        _require(identity["settings"][key] == original_settings[key], "SETTINGS")
    for key in ("start_date", "end_date", "frequency"):
        _require(identity["run_params"][key] == original_settings[key], "RUN_PARAMS")
    day, previous = original_settings["end_date"], original_settings["start_date"]
    observations = [
        checkpoint(
            events, label, at=f"{day}T{time}+08:00", previous_date=previous,
            security=original_settings["symbol"],
        )
        for label, time in (("before_trading_start", "09:00:00"), ("09:01", "09:01:00"))
    ]
    _require(observations[0]["public_mark"] == observations[1]["public_mark"], "MARK_CHANGED")
    return dict(
        schema="aq.b26-premarket-supplement-review.v1",
        source_path=str(Path(path).resolve()),
        source_sha256=expected_sha256,
        event_count=len(events),
        observations=observations,
        evidence_role="comparison_only",
        engine_input_eligible=False,
        internal_rounding_certified=False,
        independent_historical_pit_certified=False,
        day_open_admitted=False,
    )


def review(report, evidence):
    """Update attribution only; preserve every observed value and difference."""
    _require(evidence["evidence_role"] == "comparison_only", "ROLE")
    _require(evidence["engine_input_eligible"] is False, "ROLE")
    result = copy.deepcopy(report)
    rows = [
        r for r in result["comparisons"]
        if r["status"] == "DIFFERENT" and r.get("label") == "day_start"
        and r["field"] in MARK_FIELDS
    ]
    _require(len(rows) == 4 and {r["field"] for r in rows} == MARK_FIELDS, "DIFFERENCE_SCOPE")
    observed = evidence["observations"][0]
    mark_row = next(r for r in rows if r["field"] == "/positions/0/last_price")
    _require(Decimal(mark_row["jq"]) == Decimal(observed["public_mark"]), "REFERENCE_MISMATCH")
    for row in rows:
        row.update(
            previous_classification=row["classification"],
            classification="VISIBLE_PREMARKET_REFERENCE_INPUT_GAP",
            explanation=(
                "The supplement observes the JQ account mark and public adjusted prior close "
                "at the same premarket clock. AQ has no admitted independent input for this "
                "role. No rounding mechanism is proved and the numeric difference remains."
            ),
            supplement_sha256=evidence["source_sha256"],
        )
    result["differences"] = [
        r for r in result["comparisons"]
        if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    result.update(
        status="B26_REPRODUCED_BLOCKED_ON_INDEPENDENT_PREMARKET_INPUT",
        supplement_review=copy.deepcopy(evidence),
        material_mark_difference_count=len(rows),
        mark_difference_resolved=False,
        review_changes_engine_inputs=False,
        unresolved_count_semantics="unattributed fields only; zero does not mean resolved",
    )
    return result
