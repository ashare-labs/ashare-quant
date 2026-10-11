from copy import deepcopy

from ashare_quant.round_one.goal_compare import (
    correct_sell_price_fields,
    terminal_call_graph,
    terminal_review,
)


def test_terminal_review_does_not_ignore_order_in_terminal_millisecond():
    source = "def on_strategy_end(context):\n    snapshot(context)\ndef snapshot(context):\n    get_orders()\n"
    jq = [dict(seq=1, kind="snapshot", label="strategy_end", time="2024-01-05 23:59:59.001000")]
    aq = [dict(seq=1, kind="snapshot", label="strategy_end", at="2024-01-05T23:59:59+08:00")]
    assert terminal_review(source, jq, aq)["verdict"] == "FIXED_TERMINAL_READOUT_NO_ORDERING_EFFECT"
    assert (
        terminal_call_graph(source + "\ndef injected():\n    order(100)\n")["unreviewed_calls"]
        == []
    )
    assert (
        terminal_review(source.replace("get_orders()", "order(100)"), jq, aq)["verdict"]
        == "REQUIRES_REVIEW"
    )
    aq.append(dict(seq=2, kind="native_trade", at="2024-01-05T23:59:59+08:00"))
    assert terminal_review(source, jq, aq)["verdict"] == "REQUIRES_REVIEW"


def test_sell_order_mapping_uses_recorded_fill_price_and_keeps_cost_and_real_mismatch():
    row = dict(
        field="/orders/0/average_price",
        label="after_order",
        jq_event_seq=2,
        aq_event_seq=3,
        jq_present=True,
        aq_present=True,
        jq="9",
        aq="10",
        status="DIFFERENT",
    )
    report = dict(comparisons=[row], differences=[row])
    jq = [
        dict(
            seq=2,
            orders=[dict(order_id="j", is_buy=False, filled=100, price="10", avg_cost="9")],
            trades=[dict(order_id="j", amount=100, price="10")],
        )
    ]
    aq = [
        dict(
            seq=3,
            orders=[dict(order_id="a", side="SELL", filled=100, average_price="10")],
            trades=[dict(order_id="a", quantity=100, price="10")],
        )
    ]
    new = correct_sell_price_fields(report, jq, aq)
    assert not new["differences"] and new["order_field_corrections"][0]["jq_order_avg_cost"] == "9"
    assert report["differences"] == [row]
    changed = deepcopy(aq)
    changed[0]["orders"][0]["average_price"] = "11"
    changed[0]["trades"][0]["price"] = "11"
    assert (
        correct_sell_price_fields(report, jq, changed)["differences"][0]["classification"]
        == "ACTUAL_FILL_PRICE_DIFFERENCE"
    )
