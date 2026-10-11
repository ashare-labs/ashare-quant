"""Synthetic tests for owner-to-audit mapping; not real market evidence."""

from copy import deepcopy

import pytest

from ashare_quant.round_one.independent_queries import (
    compare_product,
    daily_date,
    owner_snapshot,
    selection,
    validate_scope,
)
from ashare_quant.round_one.independent_worker import denied, read


def operation(**updates):
    value = dict(
        api="get_price",
        parameters=dict(
            security="000001.XSHE",
            frequency="daily",
            fields=["close", "volume", "money"],
            start_date="2024-03-04",
            end_date="2024-03-04 15:30:00",
            fq=None,
            skip_paused=False,
        ),
        as_of="2024-03-04T15:30:00+08:00",
        request_ref_seq=5,
        label="daily",
    )
    value.update(updates)
    return value


def product():
    return dict(
        product_id="B27-daily",
        security="000001.XSHE",
        binding_sha256="a" * 64,
        start_date="2024-03-04",
        end_date="2024-03-04",
        units=dict(volume="share", money="CNY"),
        source=dict(url="synthetic://independent", sha256="b" * 64),
        rows=[
            dict(
                date="2024-03-04",
                fields={
                    "close": dict(state="value", value="10.2", origin="source"),
                    "volume": dict(state="value", value="100", origin="source"),
                    "money": dict(state="value", value="1020", origin="source"),
                },
            )
        ],
    )


def case():
    return dict(
        case_id="B27",
        files={"events.jsonl": "c" * 64},
        reference_binding={"run_url": "synthetic://jq"},
    )


def test_independent_values_drive_differences_and_owner_input_remains_unchanged():
    raw = product()
    before = deepcopy(raw)
    jq = dict(rows=[dict(index="2024-03-04 00:00:00", close="10", volume="100", money="1000")])
    value = compare_product("B27", raw, operation(), jq, case())
    assert raw == before
    diffs = {d["field"]: d["numeric_delta"] for d in value["comparison"]["differences"]}
    assert diffs == {"close": "0.2", "money": "20"}
    assert value["computed"]["right"]["rows"][0]["ratio"] == "10.2"
    assert value["owner_field_capabilities"][0]["origin"] == "source"
    assert value["case_verdict"] is None and not value["matching_eligible"]


def test_unsupported_is_a_capability_gap_not_a_zero_or_nan():
    raw = product()
    raw["rows"][0]["fields"]["close"] = dict(
        state="unsupported", value=None, reason="no independent field"
    )
    op = operation()
    op["parameters"]["fields"] = ["close"]
    snapshot, capabilities = owner_snapshot(raw, op)
    assert snapshot.status == "unsupported" and snapshot.rows == []
    assert capabilities[0]["state"] == "unsupported"
    value = compare_product(
        "C02",
        raw,
        op,
        dict(rows=[dict(index="2024-03-04 00:00:00", close={"nonfinite": "nan"})]),
        case(),
    )
    assert value["status"] == "CAPABILITY_GAP_RECORDED"
    assert value["capabilities"] == dict(left="available", right="unsupported")


def test_nan_paused_fact_is_not_mapped_to_true_or_hidden():
    raw = product()
    raw["rows"][0]["fields"]["paused"] = dict(state="value", value=True, origin="documented_fact")
    op = operation()
    op["parameters"]["fields"] = ["paused"]
    reference = dict(rows=[dict(index="2024-03-04 00:00:00", paused={"nonfinite": "nan"})])
    value = compare_product("B28", raw, op, reference, case())
    difference = value["comparison"]["differences"][0]
    assert difference["left_state"] == "nan" and difference["right_state"] == "boolean"
    assert "boolean_meaning_equal" not in difference
    reference["rows"][0]["paused"] = "1.0"
    difference = compare_product("B28", raw, op, reference, case())["comparison"]["differences"][0]
    assert difference["boolean_meaning_equal"] is True
    assert difference["equal"] is False  # exact type difference retained


@pytest.mark.parametrize(
    "change",
    [
        {"security": "600000.XSHG"},
        {"start_date": "2024-03-01"},
        {"fq": "pre"},
        {"frequency": "1m"},
        {"count": 2},
    ],
)
def test_scope_mismatch_refused(change):
    op = operation()
    op["parameters"].update(change)
    with pytest.raises(ValueError):
        validate_scope(product(), op)


def test_adjustment_basis_and_pause_policy_are_not_silently_rewritten():
    raw = product()
    raw.update(product_id="B25-adjustment", basis_date="2024-06-14", fq="pre")
    with pytest.raises(ValueError, match="BASIS_MISMATCH"):
        validate_scope(raw, operation())
    raw.update(
        product_id="B28-pause",
        security="000002.XSHE",
        skip_paused=False,
        fill_paused=False,
        rows=[],
    )
    op = operation()
    op["parameters"].update(
        security="000002.XSHE", start_date="2016-01-04", end_date="2016-01-04", fill_paused=True
    )
    with pytest.raises(ValueError, match="POLICY_MISMATCH"):
        validate_scope(raw, op)


def test_count_one_maps_only_same_day_product():
    op = operation()
    del op["parameters"]["start_date"]
    op["parameters"]["count"] = 1
    validate_scope(product(), op)
    raw = product()
    raw["start_date"] = "2024-03-01"
    with pytest.raises(ValueError, match="COUNT_SCOPE"):
        validate_scope(raw, op)


def test_minute_data_and_security_metadata_are_explicitly_unavailable():
    op = operation()
    op["parameters"]["frequency"] = "1m"
    assert selection("B27", op) == []
    assert selection("C03", operation(api="get_security_info")) == []
    with pytest.raises(ValueError, match="NON_DAILY"):
        daily_date("2024-03-04 10:00:00")


def test_worker_rejects_bad_pin_before_any_owner_import(tmp_path):
    path = tmp_path / "binding.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="OWNER_PIN_CHANGED"):
        read(path, tmp_path / "wheel.whl")
    with pytest.raises(RuntimeError, match="NETWORK_DENIED"):
        denied("example.com", 443)
