from copy import deepcopy

import pytest

from ashare_quant.round_one.carry_compare import sell_cost_evidence
from ashare_quant.round_one.carry_owner import CarryOwner


def test_cost_field_attribution_requires_matched_fills_and_prior_cost():
    history = [
        dict(
            seq=1,
            kind="snapshot",
            positions=[dict(security="000001.XSHE", total_amount=100, avg_cost="10.42")],
        )
    ]
    left = dict(
        seq=2,
        orders=[
            dict(
                is_buy=False,
                action="close",
                security="000001.XSHE",
                order_id=1,
                filled=100,
                avg_cost="10.42",
            )
        ],
        trades=[dict(order_id=1, amount=100, price="10.43")],
    )
    right = dict(
        orders=[dict(side="SELL", order_id="a", filled=100, average_price="10.43")],
        trades=[dict(order_id="a", quantity=100, price="10.43")],
    )
    assert sell_cost_evidence(left, right, history, 0)
    for modified in ["price", "quantity"]:
        changed = deepcopy(right)
        changed["trades"][0][modified] = "10.44" if modified == "price" else 99
        assert sell_cost_evidence(left, changed, history, 0) is None
    changed = deepcopy(left)
    changed["orders"][0]["avg_cost"] = "10.41"
    assert sell_cost_evidence(changed, right, history, 0) is None


def test_carry_requires_explicit_model_before_reading_owner():
    with pytest.raises(RuntimeError, match="CARRY_EXPLICIT_RESEARCH_MODEL_REQUIRED"):
        CarryOwner("DO_NOT_OPEN", "DO_NOT_START", "B14")
