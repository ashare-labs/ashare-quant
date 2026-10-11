"""Synthetic audit math tests: these never assert real-market case completion."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ashare_quant.round_one.query_math import Snapshot, compare


def snapshot(rows, **changes):
    value = dict(
        origin=dict(
            namespace="test",
            dataset_id="synthetic",
            manifest_sha256="a" * 64,
            evidence_kind="synthetic",
            source_refs=["synthetic-a"],
            quality_conclusion="synthetic math fixture; not market evidence",
        ),
        request=dict(
            api="get_price",
            parameters={"fq": None, "fill_paused": False},
            as_of="2024-03-04T15:30:00+08:00",
        ),
        status="available",
        rows=rows,
    )
    value.update(changes)
    return Snapshot.model_validate(value)


def calc(left, right, mode="rows", keys=("day",), fields=("close",)):
    return compare(left, right, mode=mode, keys=keys, fields=fields)


def test_delta_uses_values_not_expected_and_preserves_full_decimal_precision():
    long_number = "1" + "0" * 60
    left = snapshot([{"day": "a", "close": long_number}])
    right = snapshot([{"day": "a", "close": long_number + ".00000001"}])
    result = calc(left, right)
    assert Decimal(result["comparison"]["differences"][0]["numeric_delta"]) == Decimal("0.00000001")
    assert result["case_verdict"] is None
    assert result["evidence_class"] == "SYNTHETIC_ONLY"
    assert not result["matching_eligible"] and not result["historical_pit_certified"]


def test_row_order_does_not_change_keyed_comparison_and_missing_rows_are_retained():
    left = snapshot([{"day": "b", "close": "2"}, {"day": "a", "close": "1"}])
    right = snapshot([{"day": "a", "close": "1.00"}, {"day": "c", "close": "3"}])
    delta = calc(left, right)["comparison"]
    assert delta == dict(left_only=[["b"]], right_only=[["c"]], shared_keys=1, differences=[])
    assert calc(snapshot(list(reversed(left.rows))), right)["comparison"] == delta


def test_missing_null_nan_zero_and_boolean_are_distinct():
    rows = [
        {"day": "a"},
        {"day": "b", "close": None},
        {"day": "c", "close": {"nonfinite": "nan"}},
        {"day": "d", "close": "0"},
        {"day": "e", "close": False},
    ]
    left = snapshot(rows)
    right = snapshot([dict(r, close="0") for r in rows])
    result = calc(left, right, mode="field_coverage")
    assert result["computed"]["left"]["close"]["states"] == {
        "missing": 1,
        "null": 1,
        "nan": 1,
        "finite": 1,
        "boolean": 1,
    }
    assert len(result["comparison"]["differences"]) == 4


def test_membership_reports_sets_duplicates_and_metadata():
    left = snapshot([{"code": "A", "type": "stock"}, {"code": "B"}, {"code": "B"}])
    right = snapshot([{"code": "A", "type": "fund"}, {"code": "C"}])
    result = calc(left, right, "membership", ("code",), ("type",))
    assert result["computation"]["left_only"] == ["B"]
    assert result["computation"]["right_only"] == ["C"]
    assert result["computation"]["left_duplicates"] == {"B": 2}
    assert result["field_comparison"] == "BLOCKED_DUPLICATE_CODES"
    result = calc(snapshot(left.rows[:2]), right, "membership", ("code",), ("type",))
    assert result["comparison"]["differences"][0]["right"] == "fund"


def test_duplicate_keys_rejected_instead_of_silently_losing_rows():
    left = snapshot([{"day": "a"}, {"day": "a"}])
    with pytest.raises(ValueError, match="QUERY_DUPLICATE_ROW_KEY"):
        calc(left, snapshot([]))


def test_volume_money_computes_each_side_independently_and_requires_units():
    row = dict(day="a", volume="100", money="1045", low="10", high="11", avg="10.4")
    units = dict(volume="share", money="CNY")
    left = snapshot([row], units=units)
    right = snapshot([dict(row, money="1120")], units=units)
    result = calc(left, right, "volume_money")
    a, b = [result["computed"][s]["rows"][0] for s in ("left", "right")]
    assert a["ratio"] == "10.45" and a["within_high_low"] is True
    assert a["reported_avg_minus_ratio"] == "-0.05"
    assert b["ratio"] == "11.2" and b["within_high_low"] is False
    missing = calc(left, snapshot([row]), "volume_money")
    assert missing["computed"]["right"]["status"] == "UNITS_NOT_DECLARED"
    assert not missing["computed"]["left"]["independent_unit_certification"]


@pytest.mark.parametrize("volume,money", [("0", "0"), (None, "10"), ("1", None), ("-1", "10")])
def test_no_ratio_fabricated_without_valid_denominator_and_numerator(volume, money):
    left = snapshot(
        [dict(day="a", volume=volume, money=money)], units=dict(volume="share", money="CNY")
    )
    result = calc(left, left, "volume_money")
    assert result["computed"]["left"]["rows"][0]["ratio"] is None


def test_nonempty_history_uses_shanghai_day_and_keeps_window_limit():
    left = snapshot(
        [
            dict(day="2005-01-03T16:00:00+00:00", close="0", money=None),
            dict(day="2005-01-05", close="10", money="2"),
        ]
    )
    result = calc(left, left, "history", fields=("close", "money"))["computed"]["left"]
    assert result["first_nonempty"] == {"close": "2005-01-04", "money": "2005-01-05"}
    assert not result["absolute_coverage_start_certified"]


def test_freshness_retains_unknown_publication_and_uses_instant_order():
    left = snapshot(
        [dict(day="2024-03-04T09:00:00+08:00"), dict(day="2024-03-04T02:00:00+00:00")],
        captured_at="2024-03-05T02:00:00Z",
    )
    result = calc(left, left, "freshness")["computed"]["left"]
    assert result["latest_returned_label"] == "2024-03-04T02:00:00+00:00"
    assert result["capture_minus_declared_publication_seconds"] is None
    assert not result["first_publication_or_revision_completeness_certified"]
    invalid = snapshot(
        left.rows, captured_at="2024-03-05T02:00:00Z", published_at="2024-03-06T02:00:00Z"
    )
    with pytest.raises(ValueError, match="CAPTURE_PRECEDES"):
        calc(invalid, invalid, "freshness")


@pytest.mark.parametrize(
    "change",
    [
        {"parameters": {"fq": "pre", "fill_paused": False}},
        {"parameters": {"fq": None, "fill_paused": 0}},
        {"as_of": "2024-03-05T15:30:00+08:00"},
    ],
)
def test_request_semantics_must_match_including_bool_type(change):
    left = snapshot([])
    request = left.request.model_dump(mode="json") | change
    with pytest.raises(ValueError, match="QUERY_REQUEST_SCOPE_MISMATCH"):
        calc(left, snapshot([], request=request))


def test_shared_source_cannot_be_promoted_by_different_manifest_hash():
    left_origin = snapshot([]).origin.model_dump() | {"evidence_kind": "owner_data"}
    right_origin = left_origin | {
        "namespace": "jq",
        "dataset_id": "other",
        "manifest_sha256": "b" * 64,
    }
    left, right = snapshot([], origin=left_origin), snapshot([], origin=right_origin)
    assert calc(left, right)["evidence_class"] == "SHARED_SOURCE_NOT_INDEPENDENT"
    right_origin["source_refs"] = ["separate-declaration"]
    result = calc(left, snapshot([], origin=right_origin))
    assert result["evidence_class"] == "SEPARATE_DECLARED_SOURCES_REVIEW_REQUIRED"
    assert result["case_verdict"] is None


def test_unavailable_empty_and_nan_are_not_interchangeable():
    empty = snapshot([])
    unsupported = snapshot([], status="unsupported", reason="owner has no vwap capability")
    result = calc(empty, unsupported, "field_coverage", fields=("vwap",))
    assert result["status"] == "CAPABILITY_GAP_RECORDED"
    assert result["capabilities"] == {"left": "available", "right": "unsupported"}
    with pytest.raises(ValidationError, match="QUERY_UNAVAILABLE"):
        snapshot([{"day": "a"}], status="unsupported", reason="missing")


@pytest.mark.parametrize("value", [1.2, [1], {"undocumented": "value"}])
def test_cell_contract_rejects_floats_and_undocumented_structures(value):
    with pytest.raises(ValidationError):
        snapshot([dict(day="a", close=value)])


def test_timezone_required_for_logic_clock_and_row_timestamps():
    request = snapshot([]).request.model_dump(mode="json") | {"as_of": "2024-03-04T15:30:00"}
    with pytest.raises(ValidationError):
        snapshot([], request=request)
    value = snapshot([dict(day="2024-03-04T15:30:00")])
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        calc(value, value, "freshness")


def test_cli_binds_inputs_and_refuses_evidence_overwrite(tmp_path):
    left = snapshot([dict(day="a", close="10")])
    data = left.model_dump(mode="json")
    (tmp_path / "left.json").write_text(json.dumps(data))
    other = deepcopy(data)
    other["rows"][0]["close"] = "11"
    (tmp_path / "right.json").write_text(json.dumps(other))
    command = [
        sys.executable,
        "-m",
        "ashare_quant.round_one.query_math",
        "--left",
        str(tmp_path / "left.json"),
        "--right",
        str(tmp_path / "right.json"),
        "--mode",
        "rows",
        "--keys",
        "day",
        "--fields",
        "close",
        "--output",
        str(tmp_path / "result.json"),
    ]
    subprocess.run(command, check=True, capture_output=True)
    original = (tmp_path / "result.json").read_bytes()
    result = json.loads(original)
    assert (
        result["input_sha256"]["left"]
        == hashlib.sha256((tmp_path / "left.json").read_bytes()).hexdigest()
    )
    assert result["comparison"]["differences"][0]["numeric_delta"] == "1"
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (tmp_path / "result.json").read_bytes() == original
