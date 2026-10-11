"""Audit-only computations over two supplied query results, never a data source.

The AD adapter must obtain owner results independently before calling this module.
Different hashes are not proof of source independence; this module never promotes
case verdicts, certifies data quality/PIT, fills prices, or implements adjustment.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Origin(Contract):
    namespace: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_kind: Literal["owner_data", "jq_observation", "synthetic"]
    source_refs: list[str] = Field(min_length=1)
    quality_conclusion: str = Field(min_length=1)


class Request(Contract):
    api: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    as_of: AwareDatetime


class Snapshot(Contract):
    schema_version: Literal["aq.audit-query-input.v1"] = "aq.audit-query-input.v1"
    origin: Origin
    request: Request
    role: Literal["audit"] = "audit"
    matching_eligible: Literal[False] = False
    status: Literal["available", "unsupported", "unavailable", "error"]
    rows: list[dict[str, JsonValue]] = Field(default_factory=list)
    reason: str | None = None
    units: dict[str, str] = Field(default_factory=dict)
    captured_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    revision_id: str | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.status != "available" and (self.rows or not self.reason):
            raise ValueError("QUERY_UNAVAILABLE_REQUIRES_REASON_AND_NO_ROWS")
        if self.status == "available" and self.reason is not None:
            raise ValueError("QUERY_AVAILABLE_HAS_NO_ERROR_REASON")
        for row in self.rows:
            for value in row.values():
                if isinstance(value, float):
                    raise ValueError("QUERY_NUMBERS_REQUIRE_DECIMAL_STRINGS_OR_INTEGERS")
                if isinstance(value, (dict, list)) and value not in (
                    {"nonfinite": "nan"},
                    {"nonfinite": "inf"},
                    {"nonfinite": "-inf"},
                ):
                    raise ValueError("QUERY_CELL_REQUIRES_SCALAR_OR_NONFINITE_TAG")
        return self


def number(value):
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, float):
        raise ValueError("QUERY_FLOAT_NOT_ALLOWED")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def state(value, *, present=True):
    if not present:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return value["nonfinite"]
    if isinstance(value, bool):
        return "boolean"
    return "finite" if number(value) is not None else "text"


def keyed(rows, keys):
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("QUERY_UNIQUE_ROW_KEYS_REQUIRED")
    result = {}
    for row in rows:
        if any(k not in row or state(row[k]) not in {"text", "finite"} for k in keys):
            raise ValueError("QUERY_ROW_KEY_MISSING_OR_INVALID")
        identity = tuple(str(row[k]) for k in keys)
        if identity in result:
            raise ValueError("QUERY_DUPLICATE_ROW_KEY")
        result[identity] = row
    return result


def cell_difference(left, right, field):
    ls, rs = (
        state(left.get(field), present=field in left),
        state(right.get(field), present=field in right),
    )
    a, b = left.get(field), right.get(field)
    result = dict(field=field, left_state=ls, right_state=rs, left=a, right=b)
    if ls == rs == "finite":
        with localcontext() as ctx:
            av, bv = number(a), number(b)
            # Preserve every supplied decimal digit, including sub-cent deltas.
            ctx.prec = (
                max(av.adjusted(), bv.adjusted(), 0)
                - min(av.as_tuple().exponent, bv.as_tuple().exponent, 0)
                + 2
            )
            delta = bv - av
        result.update(equal=delta == 0, numeric_delta=str(delta))
    else:
        result["equal"] = ls == rs and a == b
    return result


def row_difference(left, right, keys, fields):
    li, ri = keyed(left, keys), keyed(right, keys)
    shared = sorted(li.keys() & ri.keys())
    return dict(
        left_only=[list(k) for k in sorted(li.keys() - ri.keys())],
        right_only=[list(k) for k in sorted(ri.keys() - li.keys())],
        shared_keys=len(shared),
        differences=[
            dict(key=list(k), **delta)
            for k in shared
            for f in fields
            if not (delta := cell_difference(li[k], ri[k], f))["equal"]
        ],
    )


def membership_difference(left, right, code_field):
    def codes(rows):
        values = [r.get(code_field) for r in rows]
        if any(not isinstance(v, str) or not v for v in values):
            raise ValueError("QUERY_MEMBER_CODE_REQUIRED")
        return Counter(values)

    lc, rc = codes(left), codes(right)
    left_codes, right_codes = set(lc), set(rc)
    return dict(
        left_rows=len(left),
        right_rows=len(right),
        intersection=len(left_codes & right_codes),
        left_only=sorted(left_codes - right_codes),
        right_only=sorted(right_codes - left_codes),
        left_duplicates={k: v for k, v in lc.items() if v > 1},
        right_duplicates={k: v for k, v in rc.items() if v > 1},
    )


def field_coverage(snapshot, fields):
    return {
        field: dict(
            query_status=snapshot.status,
            rows=len(snapshot.rows),
            states=dict(Counter(state(r.get(field), present=field in r) for r in snapshot.rows)),
        )
        for field in fields
    }


def volume_money(snapshot, keys):
    # Unit evidence belongs to AD. Refuse a ratio whose dimensions are undeclared.
    if snapshot.units.get("volume") != "share" or snapshot.units.get("money") != "CNY":
        return dict(status="UNITS_NOT_DECLARED", rows=[])
    rows = []
    for key, row in keyed(snapshot.rows, keys).items():
        vol, money, lo, hi, avg = [
            number(row.get(f)) for f in ("volume", "money", "low", "high", "avg")
        ]
        entry = dict(key=list(key), volume=row.get("volume"), money=row.get("money"))
        if vol is None or money is None or vol <= 0 or money < 0:
            entry.update(status="RATIO_UNAVAILABLE", ratio=None)
        else:
            with localcontext() as ctx:
                ctx.prec = 50
                ratio = money / vol
                entry.update(
                    status="COMPUTED",
                    ratio=str(ratio),
                    within_high_low=None if lo is None or hi is None else lo <= ratio <= hi,
                    reported_avg_minus_ratio=None if avg is None else str(avg - ratio),
                )
        rows.append(entry)
    return dict(
        status="COMPUTED_FROM_OWNER_DECLARED_UNITS",
        independent_unit_certification=False,
        division_precision_digits=50,
        rows=rows,
    )


def clock(value):
    if not isinstance(value, str):
        raise ValueError("QUERY_DATE_FIELD_REQUIRED")
    shanghai = ZoneInfo("Asia/Shanghai")
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=shanghai)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("QUERY_TIMESTAMP_TIMEZONE_REQUIRED")
    return dt.astimezone(shanghai)


def earliest(snapshot, fields, clock_field):
    dates = [(clock(row.get(clock_field)).date().isoformat(), row) for row in snapshot.rows]
    return dict(
        first_nonempty={
            f: min((day for day, row in dates if number(row.get(f)) is not None), default=None)
            for f in fields
        },
        rows=len(dates),
        absolute_coverage_start_certified=False,
    )


def freshness(snapshot, clock_field):
    dates = [r.get(clock_field) for r in snapshot.rows]
    for value in dates:
        clock(value)
    lag = None
    if snapshot.published_at and snapshot.captured_at:
        lag = Decimal(str((snapshot.captured_at - snapshot.published_at).total_seconds()))
        if lag < 0:
            raise ValueError("QUERY_CAPTURE_PRECEDES_PUBLISHED_AT")
    return dict(
        latest_returned_label=max(dates, key=clock, default=None),
        logical_as_of=snapshot.request.as_of.isoformat(),
        captured_at=None if snapshot.captured_at is None else snapshot.captured_at.isoformat(),
        published_at=None if snapshot.published_at is None else snapshot.published_at.isoformat(),
        revision_id=snapshot.revision_id,
        capture_minus_declared_publication_seconds=None if lag is None else str(lag),
        first_publication_or_revision_completeness_certified=False,
    )


MODES = ("rows", "membership", "field_coverage", "volume_money", "history", "freshness")


def compare(left: Snapshot, right: Snapshot, *, mode, keys, fields):
    if (
        mode not in MODES
        or not keys
        or not fields
        or len(fields) != len(set(fields))
        or len(keys) != len(set(keys))
    ):
        raise ValueError("QUERY_COMPARISON_ARGUMENTS_INVALID")
    # Python dict equality conflates True with 1; request types are part of scope.
    if (
        left.request.api != right.request.api
        or left.request.as_of != right.request.as_of
        or json.dumps(left.request.parameters, sort_keys=True, allow_nan=False)
        != json.dumps(right.request.parameters, sort_keys=True, allow_nan=False)
    ):
        raise ValueError("QUERY_REQUEST_SCOPE_MISMATCH")
    shared_sources = sorted(set(left.origin.source_refs) & set(right.origin.source_refs))
    shared_identity = (left.origin.namespace, left.origin.dataset_id) == (
        right.origin.namespace,
        right.origin.dataset_id,
    )
    evidence = (
        "SYNTHETIC_ONLY"
        if "synthetic" in {left.origin.evidence_kind, right.origin.evidence_kind}
        else (
            "SHARED_SOURCE_NOT_INDEPENDENT"
            if shared_sources or shared_identity
            else "SEPARATE_DECLARED_SOURCES_REVIEW_REQUIRED"
        )
    )
    result = dict(
        schema="aq.audit-query-computation.v1",
        status="COMPUTED_NOT_ADJUDICATED",
        mode=mode,
        request=left.request.model_dump(mode="json"),
        left_origin=left.origin.model_dump(),
        right_origin=right.origin.model_dump(),
        evidence_class=evidence,
        shared_source_refs=shared_sources,
        case_verdict=None,
        historical_pit_certified=False,
        matching_eligible=False,
        capabilities=dict(left=left.status, right=right.status),
        reasons=dict(left=left.reason, right=right.reason),
    )
    if left.status != "available" or right.status != "available":
        result["status"] = "CAPABILITY_GAP_RECORDED"
        result["field_coverage"] = dict(
            left=field_coverage(left, fields), right=field_coverage(right, fields)
        )
        return result
    if mode == "membership":
        if len(keys) != 1:
            raise ValueError("QUERY_SINGLE_CODE_KEY_REQUIRED")
        result["computation"] = membership_difference(left.rows, right.rows, keys[0])
        if result["computation"]["left_duplicates"] or result["computation"]["right_duplicates"]:
            result["field_comparison"] = "BLOCKED_DUPLICATE_CODES"
        else:
            result["comparison"] = row_difference(left.rows, right.rows, keys, fields)
        return result
    result["comparison"] = row_difference(left.rows, right.rows, keys, fields)
    derive = {
        "field_coverage": lambda s: field_coverage(s, fields),
        "volume_money": lambda s: volume_money(s, keys),
        "history": lambda s: earliest(s, fields, keys[0]),
        "freshness": lambda s: freshness(s, keys[0]),
    }.get(mode)
    if derive:
        result["computed"] = dict(left=derive(left), right=derive(right))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--keys", nargs="+", required=True)
    parser.add_argument("--fields", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = [p.read_bytes() for p in (args.left, args.right)]
    snapshots = [Snapshot.model_validate_json(b) for b in raw]
    result = compare(*snapshots, mode=args.mode, keys=args.keys, fields=args.fields)
    result["input_sha256"] = dict(
        zip(("left", "right"), (hashlib.sha256(b).hexdigest() for b in raw))
    )
    result["calculator_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # A completed calculation never overwrites earlier evidence.
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print(json.dumps(dict(status=result["status"], output=str(args.output.resolve()))))


if __name__ == "__main__":
    main()
