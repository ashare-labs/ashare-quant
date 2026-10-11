from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from ashare_quant.round_one.dividend_cases import CASES
from ashare_quant.round_one.dividend_owner import DividendOwner
from ashare_quant.round_one.premarket import MODEL, admit, apply_reference_mark


def envelope():
    return dict(
        schema="ad.premarket-reference-result.v1",
        status="available",
        security="000002.XSHE",
        role="premarket_reference_reconstruction",
        policy="sina_native_prevclose_known_cash_research.v1",
        model_available_at="2024-03-04T09:00:00+08:00",
        historical_available_at=None,
        historical_pit=False,
        strategy_visible=False,
        matching_eligible=False,
        current_quote=False,
        row=dict(
            ex_date="2024-03-04",
            previous_session="2024-03-01",
            reference_price="12.65",
            gross_cash_per_share="0.347",
            raw_previous_close="13",
        ),
    )


def admission(value):
    return admit(
        value,
        security="000002.XSHE",
        as_of="2024-03-04T09:00:00+08:00",
        fact=dict(ex_date="2024-03-04", record_date="2024-03-01", gross_cash_per_share="0.347"),
        raw_previous_close="13",
    )


def test_independent_price_is_used_without_rounding_or_cost_subtraction():
    assert admission(envelope()) == 12.65
    changed = deepcopy(envelope())
    changed["row"]["reference_price"] = "12.66"
    assert admission(changed) == 12.66  # admitted source value, no expected-answer constant


@pytest.mark.parametrize(
    "key,value",
    [
        ("strategy_visible", True),
        ("matching_eligible", True),
        ("current_quote", True),
        ("historical_pit", True),
        ("historical_available_at", "2024-03-04T09:00:00+08:00"),
        ("model_available_at", "2024-03-04T15:00:00+08:00"),
        ("security", "000001.XSHE"),
    ],
)
def test_widened_capabilities_and_clock_refused(key, value):
    with pytest.raises(RuntimeError, match="ENVELOPE"):
        admission(envelope() | {key: value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("raw_previous_close", "12.9"),
        ("gross_cash_per_share", "0.4"),
        ("previous_session", "2024-02-29"),
        ("ex_date", "2024-03-05"),
    ],
)
def test_native_basis_conflicts_refused(key, value):
    value_ = envelope()
    value_["row"][key] = value
    with pytest.raises(RuntimeError, match="BASIS_CONFLICT"):
        admission(value_)


def test_owner_refuses_wrong_phase_or_consumer_before_transport():
    owner = object.__new__(DividendOwner)
    owner.case = CASES["B26"]
    owner.model = dict(
        schema=MODEL, premarket_reference=dict(model_available_at="2024-06-14T09:00:00+08:00")
    )
    with pytest.raises(RuntimeError, match="ENGINE_ONLY"):
        owner.read(
            "premarket_reference",
            datetime.fromisoformat("2024-06-14T09:00:00+08:00"),
            consumer="strategy",
        )
    for at in (
        "2024-06-13T09:00:00+08:00",
        "2024-06-14T08:59:59+08:00",
        "2024-06-14T10:00:00+08:00",
    ):
        with pytest.raises(RuntimeError, match="PREMARKET_CLOCK"):
            owner.read("premarket_reference", datetime.fromisoformat(at))


def test_only_last_price_can_change_and_subsequent_mark_is_not_sticky():
    from rqalpha.portfolio.position import Position

    position = SimpleNamespace(_unadjusted_prev_close=13, _last_price=12.653)
    position.get_state = lambda: dict(quantity=200, avg_price=12.713, prev_close=12.653)
    position.update_last_price = lambda price: Position.update_last_price(position, price)
    before = position.get_state()
    apply_reference_mark(position, 12.65)
    assert position._last_price == 12.65 and position.get_state() == before
    position.update_last_price(12.78)
    assert position._last_price == 12.78 and position.get_state() == before


def test_implicit_reference_model_cannot_be_enabled():
    with pytest.raises(RuntimeError, match="PREMARKET_OWNER_INPUTS_REQUIRED"):
        DividendOwner("DO_NOT_READ", "DO_NOT_START", "B26", carry_model=MODEL)


@pytest.mark.parametrize("ratio", ["0.3", "0.7"])
def test_capitalization_admission_requires_matching_public_ratio(ratio):
    p = envelope()
    p["policy"] = "sina_native_prevclose_known_cash_capitalization_research.v1"
    p["row"]["capitalization_share_ratio"] = ratio
    fact = dict(
        ex_date="2024-03-04",
        record_date="2024-03-01",
        gross_cash_per_share="0.347",
        capitalization_share_ratio=ratio,
    )
    args = dict(
        security="000002.XSHE", as_of=p["model_available_at"], fact=fact, raw_previous_close="13"
    )
    assert admit(p, **args) == 12.65
    p["row"]["capitalization_share_ratio"] = "0.8"
    with pytest.raises(RuntimeError, match="BASIS_CONFLICT"):
        admit(p, **args)
    p["policy"] = "sina_native_prevclose_known_cash_research.v1"
    with pytest.raises(RuntimeError, match="ENVELOPE"):
        admit(p, **args)
