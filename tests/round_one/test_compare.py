from copy import deepcopy

import pytest

from ashare_quant.round_one.compare import (
    attribute,
    fields,
    normalize_order,
    reservation_policy_proven,
    semantic_status,
)


def jq_order():
    return dict(
        security="000001.XSHE",
        amount=100,
        filled=100,
        avg_cost="10.42",
        commission="0",
        add_time="2024-03-04 10:00:00",
        status="held",
    )


def test_style_missing_is_unknown_not_market():
    assert "style" not in normalize_order(jq_order(), "jq")


def test_b08_style_text_extraction_never_executes_representation():
    order = jq_order()
    order["native_repr"] = (
        "UserOrder({'style': LimitOrderStyle: _limit_price=10.62, 'comment': '__import__'})"
    )
    result = normalize_order(order, "jq")
    assert result["style"] == "limit"
    assert result["limit_price"] == "10.62"


def test_small_difference_is_retained_and_missing_is_not_zero():
    rows = fields({"cash": "999.0000000001", "unknown": None}, {"cash": "999", "unknown": 0}, {})
    assert rows[0]["status"] == "DIFFERENT"
    assert rows[0]["classification"] == "FLOAT_REPRESENTATION_DIFFERENCE_RETAINED"
    assert rows[1]["status"] == "DIFFERENT"


@pytest.mark.parametrize(
    "path", ["/account/available_cash", "/positions/0/quantity", "/positions/0/market_value"]
)
def test_terminal_unexpected_differences_are_not_blanket_excused(path):
    row = dict(field=path, status="DIFFERENT", jq="100", aq="99", jq_present=True, aq_present=True)
    attribute(row, "strategy_end", "B09")
    assert row["classification"] == "REQUIRES_INVESTIGATION"


def test_arbitrary_cash_difference_not_attributed_to_reservation():
    row = dict(
        field="/account/frozen_cash",
        status="DIFFERENT",
        jq="890070",
        aq="999",
        jq_present=True,
        aq_present=True,
    )
    attribute(row, "before_cancel", "B10")
    assert row["classification"] == "REQUIRES_INVESTIGATION"


def test_reservation_attribution_requires_both_cash_equations():
    order = dict(
        quantity=100000,
        filled=16500,
        remaining=83500,
        limit_price="10.62",
        average_price="10.42",
        transaction_cost="0",
        style="limit",
    )
    jq = dict(orders=[order], account=dict(frozen_cash="890070", available_cash="98938000"))
    aq = dict(orders=[order], account=dict(frozen_cash="886770", available_cash="98941300"))
    assert reservation_policy_proven(jq, aq, "100000000")
    jq = deepcopy(jq)
    jq["orders"][0]["transaction_cost"] = "0.0"
    assert reservation_policy_proven(jq, aq, "100000000")
    changed = deepcopy(aq)
    changed["account"]["available_cash"] = "98941301"
    assert not reservation_policy_proven(jq, changed, "100000000")


@pytest.mark.parametrize(
    "jq,native,filled,qty,status",
    [
        ("held", "FILLED", 100, 100, "filled"),
        ("filled", "ACTIVE", 10, 100, "partial_active"),
        ("new", "ACTIVE", 0, 100, "accepted_unfilled"),
        ("canceled", "CANCELLED", 10, 100, "cancelled"),
    ],
)
def test_explicit_status_mapping(jq, native, filled, qty, status):
    assert semantic_status(jq, filled, qty) == semantic_status(native, filled, qty) == status


def test_portfolio_intent_compares_raw_requested_not_native_order_size():
    from ashare_quant.round_one.compare import normalize_intent_pair

    left, right = normalize_intent_pair(
        dict(api="order", requested=200, symbol="000001.XSHE", time="2024-03-04 10:00:00"),
        dict(
            api="order",
            requested="200",
            security="000001.XSHE",
            at="2024-03-04T10:00:00+08:00",
            style="market",
        ),
    )
    assert left == right
    assert right["requested"] == "200"
    assert "style" not in left
    right["requested"] = "100"
    assert any(r["status"] == "DIFFERENT" for r in fields(left, right, {}))


def test_boolean_status_is_not_numeric_zero():
    assert fields({"paused": False}, {"paused": 0}, {})[0]["status"] == "DIFFERENT"


def test_market_field_change_remains_visible():
    from ashare_quant.round_one.market_compare import compare_markets

    jq = [
        dict(
            kind="current_data",
            seq=1,
            label="probe",
            time="2024-03-04 10:00:00",
            symbol="000001.XSHE",
            data=dict(
                day_open=10, high_limit=11, low_limit=9, last_price=10, paused=False, is_st=False
            ),
        )
    ]
    aq = [
        dict(
            kind="market_observation",
            seq=2,
            label="probe",
            at="2024-03-04T10:00:00+08:00",
            history=dict(security="000001.XSHE"),
            quote=dict(
                day_open="10",
                upper_limit="11",
                lower_limit="9",
                last="10",
                suspended=False,
                is_st=False,
            ),
        )
    ]
    assert compare_markets(jq, aq)["status"] == "MARKET_QUERY_FIELDS_MATCH"
    aq[0]["quote"]["last"] = "10.01"
    value = compare_markets(jq, aq)
    assert value["status"] == "MARKET_QUERY_FIELDS_DIFFER"
    assert value["differences"][0]["field"] == "/quote/last_price"


def test_pending_view_attribution_requires_equal_reserved_state_and_no_positions():
    import copy

    from ashare_quant.round_one.compare import pending_order_view_proven

    left = dict(
        open_order_count=0,
        orders=[dict(filled=0)],
        positions=[],
        trades=[],
        account=dict(available_cash="900", frozen_cash="100"),
    )
    right = copy.deepcopy(left)
    right["open_order_count"] = 1
    assert pending_order_view_proven(left, right)
    right["account"]["available_cash"] = "899"
    assert not pending_order_view_proven(left, right)
    right["account"]["available_cash"] = "900"
    left["positions"] = right["positions"] = [dict(quantity=100)]
    assert not pending_order_view_proven(left, right)
