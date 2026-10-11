from copy import deepcopy
from datetime import datetime

import pytest

from ashare_quant.round_one.dividend_cases import CASES
from ashare_quant.round_one.dividend_owner import DividendOwner, check_known_actions


def test_dividend_requires_explicit_model_before_reading_any_files():
    with pytest.raises(RuntimeError, match="B26_EXPLICIT_RESEARCH_MODEL_REQUIRED"):
        DividendOwner("DO_NOT_READ", "DO_NOT_START", "B26")


def test_dividend_cannot_bypass_known_conflicting_action():
    fact = dict(
        security="000001.XSHE",
        event_type="cash_dividend",
        announcement_date="2024-06-06",
        record_date="2024-06-13",
        ex_date="2024-06-14",
        payment_date="2024-06-14",
        gross_cash_per_share="0.7190",
        bonus_share_ratio="0",
        capitalization_share_ratio="0",
    )
    facts = dict(
        actions=dict(
            known_ordinary_cash_schedule=dict(events=[dict(fact)]),
            provider_effective_actions=dict(
                events=[dict(category_raw=1, effective_date="2024-06-14")]
            ),
            provider_split_consolidation=dict(events=[]),
        )
    )
    check_known_actions(facts, fact)
    conflict = deepcopy(facts)
    conflict["actions"]["known_ordinary_cash_schedule"]["events"][0]["payment_date"] = "2024-06-17"
    with pytest.raises(RuntimeError, match="B26_CASH_FACT_CONFLICT"):
        check_known_actions(conflict, fact)
    conflict = deepcopy(facts)
    conflict["actions"]["provider_split_consolidation"]["events"] = [
        dict(effective_date="2024-06-14")
    ]
    with pytest.raises(RuntimeError, match="B26_OTHER_KNOWN_ACTION"):
        check_known_actions(conflict, fact)


def test_dividend_future_close_and_point_strategy_access_refuse_before_transport():
    owner = object.__new__(DividendOwner)
    owner.case = CASES["B26"]
    owner.call = lambda *a, **k: pytest.fail("forbidden request reached owner transport")
    for op, at, role, code in [
        ("valuation", "2024-06-14T10:00:00+08:00", "engine", "B26_DAILY_CLOSE_NOT_YET_OBSERVED"),
        ("point", "2024-06-14T10:00:00+08:00", "strategy", "B26_ENGINE_ONLY_ROLE"),
        ("previous_close", "2024-06-14T09:00:00+08:00", "strategy", "B26_ENGINE_ONLY_ROLE"),
        ("cash_fact", "2024-06-14T09:00:00+08:00", "strategy", "B26_ENGINE_ONLY_ROLE"),
        ("quote", "2024-06-14T10:00:00+08:00", "strategy", "B26_NO_EX_DAY_STRATEGY_QUOTE_OR_BAR"),
        ("bar", "2024-06-14T10:00:00+08:00", "engine", "B26_NO_EX_DAY_STRATEGY_QUOTE_OR_BAR"),
    ]:
        with pytest.raises(RuntimeError, match=code):
            owner.read(op, datetime.fromisoformat(at), consumer=role)


def test_posting_attribution_requires_actual_matching_quantity_and_gross_credit():
    from ashare_quant.round_one.dividend_compare import posting_evidence

    before = dict(positions=[dict(total_amount=1000)], portfolio=dict(available_cash="989140"))
    after = dict(
        positions=[dict(total_amount=1000, price="10.08")],
        portfolio=dict(available_cash="989715.2"),
    )
    native = dict(
        source_gross_per_share="0.7190",
        before=dict(cash="989140", positions=[dict(quantity=1000, last_price="10.8")]),
        after=dict(cash="989859", positions=[dict(quantity=1000, last_price="10.081")]),
    )
    assert posting_evidence(before, after, native)["cash_difference"] == "143.8"
    changed = deepcopy(native)
    changed["after"]["cash"] = "989858"
    assert posting_evidence(before, after, changed) is None
    changed = deepcopy(after)
    changed["positions"][0]["total_amount"] = 900
    assert posting_evidence(before, changed, native) is None
    changed = deepcopy(native)
    changed["after"]["positions"][0]["last_price"] = "10.08"
    assert posting_evidence(before, after, changed) is None


def test_simulated_tax_is_not_a_price_or_cost_adjustment():
    from ashare_quant.round_one.dividend_tax import net_payable

    assert net_payable(70) == 56  # artificial amount, not B26 reference
    assert net_payable(1.125) == 0.9
    assert net_payable(0) == 0
    for bad in ["NaN", "Infinity", "-1"]:
        with pytest.raises(RuntimeError, match="PAYABLE_INVALID"):
            net_payable(bad)
