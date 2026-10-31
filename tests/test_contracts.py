from datetime import date

import pytest
from pydantic import ValidationError

from ashare_quant.contracts import Account, CorporateActionFact, DataQuery, EngineStep, Position


@pytest.mark.parametrize("cash", [100, 100.0, "NaN", "Infinity", "-1", "1e3", "01.00"])
def test_money_requires_finite_nonnegative_decimal_string(cash):
    with pytest.raises(ValidationError):
        Account(cash=cash, equity="100.00")


def test_decimal_roundtrip_is_exact():
    value = "12345678901234567890.12345678"
    assert Account.model_validate_json(Account(cash=value, equity=value).model_dump_json()).cash == value


def test_position_cannot_sell_more_than_held():
    with pytest.raises(ValidationError):
        Position(security="600000.XSHG", quantity=100, sellable=101)


def test_snapshot_cannot_overreserve_cash_or_repeat_security():
    with pytest.raises(ValidationError):
        Account(cash="100.00", frozen_cash="101.00", equity="100.00")
    position = Position(security="600000.XSHG", quantity=100, sellable=100)
    with pytest.raises(ValidationError):
        Account(cash="100.00", equity="100.00", positions=(position, position))


def query(**overrides):
    return dict(dataset_id="data-1", securities=["600000.XSHG"], frequency="1m",
                start="2026-09-28T09:30:00+08:00", end="2026-09-28T10:00:00+08:00",
                as_of="2026-09-28T10:00:00+08:00", visibility="verified") | overrides


@pytest.mark.parametrize("override", [
    {"as_of": "2026-09-28T10:00:00"},
    {"as_of": "2026-09-28T09:59:00+08:00"},
    {"start": "2026-09-28T10:01:00+08:00"},
    {"securities": ["300001.XSHE"]},
    {"securities": ["600000.XSHG", "600000.XSHG"]},
    {"silently_fill_missing": True},
])
def test_data_clock_pool_and_extra_fields_fail_closed(override):
    with pytest.raises(ValidationError):
        DataQuery(**query(**override))


def test_trade_day_uses_shanghai_not_utc():
    event = {
        "logical_at": "2026-09-27T17:00:00Z",
        "trading_day": "2026-09-28",
        "kind": "open",
        "account": {
            "cash": "10000.00",
            "frozen_cash": "0.00",
            "equity": "10000.00",
            "positions": [],
        },
        "message": "Synthetic session starts",
        "checkpoint": {},
        "sequence": 1,
    }
    assert EngineStep(**event).trading_day == date(2026, 9, 28)
    event["trading_day"] = "2026-09-27"
    with pytest.raises(ValidationError):
        EngineStep(**event)


def test_company_action_fact_has_separate_record_ex_pay_dates():
    fact = CorporateActionFact(
        action_id="dividend-1",
        dataset_id="synthetic:corporate_action:v1",
        security="600000.XSHG",
        available_at="2026-09-28T08:00:00+08:00",
        record_date="2026-09-28",
        ex_date="2026-09-29",
        pay_date="2026-09-30",
        cash_per_share="1.00",
        source_reference="synthetic manual declaration; production facts must come from A数达",
    )
    assert fact.record_date < fact.ex_date < fact.pay_date
