"""Execution policy invariants use quantities different from acceptance answers."""

import pytest
from ashare_quant.second_batch.policies import resting_capacity, sell_quantity


def test_mixed_inventory_preserves_raw_intent_but_never_sells_todays_shares():
    assert sell_quantity(-700, 400) == -400
    assert sell_quantity(-300, 400) == -300
    assert sell_quantity(-121, 121) == -121
    assert sell_quantity(-700, 0) == 0
    assert sell_quantity(700, 400) == 700


@pytest.mark.parametrize("requested,closable", [(True, 100), (-100, -1), (-1, 1.1)])
def test_invalid_native_quantity_is_not_coerced(requested, closable):
    with pytest.raises(ValueError):
        sell_quantity(requested, closable)


def test_resting_order_may_fill_integer_odd_lot_and_shares_capacity_across_orders():
    assert resting_capacity("2500999", "0.0001") == 250
    assert resting_capacity("2500999", "0.0001", 123) == 127
    assert resting_capacity("2500999", "0.0001", 300) == 0
    assert resting_capacity("9999", "0.0001") == 0
    assert resting_capacity("12345", "1") == 12345


@pytest.mark.parametrize(
    "volume,ratio,used",
    [
        ("NaN", "0.1", 0),
        ("100", "Inf", 0),
        ("-1", "0.1", 0),
        ("100", "0", 0),
        ("100", "1.1", 0),
        ("100", "0.1", -1),
    ],
)
def test_capacity_refuses_nonfinite_invalid_and_negative_inputs(volume, ratio, used):
    with pytest.raises(ValueError):
        resting_capacity(volume, ratio, used)
