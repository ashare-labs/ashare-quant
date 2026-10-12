from decimal import Decimal, localcontext

import pytest

from ashare_quant.daily_portfolio.signals import decide, order_plan, scheduled

IDS = [
    "000001.XSHE",
    "000333.XSHE",
    "000651.XSHE",
    "000858.XSHE",
    "600000.XSHG",
    "600036.XSHG",
    "600519.XSHG",
    "601318.XSHG",
]


def test_hand_calculated_equal_weight_and_trend_equality():
    history = {s: ["10"] * 120 for s in IDS}
    equal = decide("equal-weight", history, "10000")
    assert equal["targets"] == dict.fromkeys(IDS, 100)  # 1225 each -> 100 shares
    assert set(equal["weights"].values()) == {"0.1225"}
    assert decide("trend", history, "10000")["targets"] == dict.fromkeys(IDS, 0)
    history[IDS[-1]][-1] = "11"
    trend = decide("trend", history, "10000")
    assert trend["selected"] == [IDS[-1]]
    assert trend["targets"][IDS[-1]] == 100
    assert sum(Decimal(w) for w in trend["weights"].values()) == Decimal("0.1225")


def test_sixty_intervals_ties_and_negative_scores():
    history = {s: ["20"] * 120 for s in reversed(IDS)}
    for values in history.values():
        values[-1] = "10"
        values[-60] = "999"  # The denominator must be [-61], not [-60].
    signal = decide("momentum", history, "10000")
    assert signal["selected"] == IDS[:3]
    assert set(signal["scores"].values()) == {"-0.5"}
    assert [signal["targets"][s] for s in IDS] == [300, 300, 300, 0, 0, 0, 0, 0]


def test_window_calendar_boundaries_and_sell_first():
    assert scheduled("equal-weight", "2020-02-03", "2020-01-23", False)
    assert not scheduled("equal-weight", "2020-02-04", "2020-02-03", False)
    assert scheduled("momentum", "2024-02-19", "2024-02-08", False)
    assert scheduled("momentum", "2022-03-01", "2022-02-28", True)
    assert not scheduled("momentum", "2022-03-01", "2022-02-28", False)
    assert order_plan({"A": 100, "B": 0, "C": 0}, {"A": 0, "B": 200, "C": 100}) == [
        ("B", -200),
        ("C", -100),
        ("A", 100),
    ]


def test_missing_prices_are_not_skipped_or_filled():
    h = {s: ["10"] * 120 for s in IDS}
    h[IDS[0]][-1] = "NaN"
    with pytest.raises(ValueError, match="INVALID_SIGNAL"):
        decide("momentum", h, "10000")
    h[IDS[0]] = ["10"] * 119
    with pytest.raises(ValueError, match="120_SESSION"):
        decide("trend", h, "10000")


def test_native_decimal_context_cannot_change_a_frozen_signal():
    history = {s: ["10.000001"] * 120 for s in IDS}
    expected = decide("equal-weight", history, "1000000.00000001")
    with localcontext(prec=10):
        assert decide("equal-weight", history, "1000000.00000001") == expected


def test_weekly_reversal_five_intervals_bottom_two_and_ties():
    history = {s: ["20"] * 120 for s in reversed(IDS[:4])}
    for values in history.values():
        values[-1] = "10"
        values[-5] = "999"  # Five return intervals require six closing prices.
    history[IDS[3]][-1] = "30"
    signal = decide("weekly-reversal", history, "10000")
    assert signal["selected"] == IDS[:2]
    assert signal["scores"][IDS[0]] == "-0.5"
    assert signal["targets"] == {IDS[0]: 400, IDS[1]: 400, IDS[2]: 0, IDS[3]: 0}
    assert scheduled("weekly-reversal", "2020-02-03", "2020-01-23", False)
    assert not scheduled("weekly-reversal", "2020-02-04", "2020-02-03", False)


def test_daily_equal_weight_varied_universe_and_high_price_lot():
    history = {"A": ["10"] * 120, "B": ["1000"] * 120}
    assert decide("daily-equal-weight", history, "10000")["targets"] == {"A": 400, "B": 0}
    assert scheduled("daily-equal-weight", "2020-02-04", "2020-02-03", False)
