"""Synthetic code sets test comparison mechanics, not historical membership."""

from copy import deepcopy

import pytest

from ashare_quant.round_one.independent_index import compare_product, owner_snapshot
from ashare_quant.round_one.index_worker import ROLE, denied, read


def product():
    return dict(
        product_id="C04-members",
        security="000905.XSHG",
        as_of_date="2019-02-01",
        binding_sha256="a" * 64,
        role=ROLE,
        historical_pit=False,
        strategy_visible=False,
        matching_feed=False,
        official_chain_verified=False,
        complete_membership_certified=False,
        historical_name_verified=False,
        source=dict(url="synthetic://archive", sha256="b" * 64, quality="uncertified"),
        rows=[
            dict(security="000003.XSHE", source_name="current name"),
            dict(security="600003.XSHG", source_name="old code retained"),
        ],
    )


def operation():
    return dict(
        api="get_index_stocks",
        parameters=dict(index_symbol="000905.XSHG", date="2019-02-01"),
        as_of="2020-02-03T15:30:00+08:00",
        request_ref_seq=4,
    )


def compare(raw):
    return compare_product(
        raw,
        operation(),
        dict(rows=[dict(code="600003.XSHG"), dict(code="600999.XSHG")]),
        dict(
            case_id="synthetic",
            files={"events.jsonl": "c" * 64},
            reference_binding=dict(run_url="synthetic://jq"),
        ),
        allow_uncertified=True,
    )


def test_actual_code_differences_preserved_without_certification_or_name_rewrite():
    raw = product()
    before = deepcopy(raw)
    result = compare(raw)
    assert raw == before
    assert result["computation"]["intersection"] == 1
    assert result["computation"]["left_only"] == ["600999.XSHG"]
    assert result["computation"]["right_only"] == ["000003.XSHE"]
    assert result["case_verdict"] is None
    assert result["historical_pit_certified"] is False
    assert result["matching_eligible"] is False
    assert result["names_compared"] is False
    assert result["future_identity_rewrites"] is False


def test_duplicates_are_retained_and_block_unique_row_comparison():
    raw = product()
    raw["rows"].append(deepcopy(raw["rows"][0]))
    result = compare(raw)
    assert result["computation"]["right_duplicates"] == {"000003.XSHE": 2}
    assert result["field_comparison"] == "BLOCKED_DUPLICATE_CODES"


def test_explicit_uncertified_opt_in_required():
    for flag in (False, None, 1):
        with pytest.raises(ValueError, match="OPT_IN"):
            owner_snapshot(product(), operation(), allow_uncertified=flag)


@pytest.mark.parametrize(
    "change", [dict(date="2019-02-02"), dict(index_symbol="000300.XSHG"), dict(fields=["name"])]
)
def test_scope_cannot_be_changed(change):
    op = operation()
    op["parameters"].update(change)
    with pytest.raises(ValueError, match="SCOPE"):
        owner_snapshot(product(), op, allow_uncertified=True)


@pytest.mark.parametrize("field", ["strategy_visible", "historical_pit", "official_chain_verified"])
def test_candidate_cannot_be_silently_promoted(field):
    raw = product()
    raw[field] = True
    with pytest.raises(ValueError, match="CAPABILITY"):
        compare(raw)


def test_worker_checks_pin_before_import_and_refuses_network(tmp_path):
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    with pytest.raises(ValueError, match="PIN_CHANGED"):
        read(binding, tmp_path / "unread-wheel.whl")
    with pytest.raises(RuntimeError, match="NETWORK_DENIED"):
        denied("example.com", 443)
