"""Regression for mathematical ties and distinct factors below display precision."""

import random
from decimal import localcontext
from fractions import Fraction

import pytest

from ashare_quant.portfolio.signals import decide

IDS = [
    "000001.XSHE",
    "000002.XSHE",
    "000651.XSHE",
    "600000.XSHG",
    "600036.XSHG",
    "600519.XSHG",
]
STRATEGIES = ["low-volatility", "momentum-low-volatility"]


def variance(values):
    returns = [Fraction(b) / Fraction(a) - 1 for a, b in zip(values[-21:-1], values[-20:])]
    # Independent second-moment identity, rather than production's centered sum.
    return sum(r * r for r in returns) / 20 - (sum(returns) / 20) ** 2


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("precision", [9, 28, 80])
def test_permuted_equal_nonzero_variance_uses_code_tie_break(strategy, precision):
    rng = random.Random(971)
    peaks = rng.sample(range(920, 1081), 10)
    history = {}
    for sid in IDS:
        ordered = peaks[:]
        rng.shuffle(ordered)
        # Every two-price cycle returns to 10. Permuting cycles preserves the
        # exact return multiset, nonzero variance and zero 60-session momentum.
        history[sid] = ["10"] * 100 + [
            f"{v // 100}.{v % 100:02d}" for peak in ordered for v in (peak, 1000)
        ]
    exact_variances = {variance(values) for values in history.values()}
    assert len(exact_variances) == 1 and next(iter(exact_variances)) > 0
    with localcontext(prec=precision):
        result = decide(strategy, history, "1000000")
        assert decide(strategy, dict(reversed(list(history.items()))), "1000000") == result
    assert result["selected"] == IDS[:3]
    assert result["targets"] == {sid: 32600 if sid in IDS[:3] else 0 for sid in IDS}
    assert len(set(result["scores"].values())) == 1
    if strategy == "momentum-low-volatility":
        assert set(result["scores"].values()) == {"0"}


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_distinct_nonzero_variances_are_not_merged_at_display_precision(strategy):
    ids = IDS[:4]
    history = {
        sid: ["10"] * 100 + ["11." + "0" * 59 + str(4 - i), "10"] * 10 for i, sid in enumerate(ids)
    }
    factors = [variance(history[sid]) for sid in ids]
    assert factors[0] > factors[1] > factors[2] > factors[3] > 0
    result = decide(strategy, history, "1000000")
    assert result["selected"] == list(reversed(ids))[:3]
    assert result["targets"][ids[0]] == 0
    if strategy == "low-volatility":
        # Rounded display equality must not be used for selection or epsilon ties.
        assert len(set(result["scores"].values())) == 1
    else:
        assert result["scores"] == {sid: str(3 - i) for i, sid in enumerate(ids)}


def test_momentum_competition_ranks_use_exact_ratios_too():
    history = {sid: ["10"] * 120 for sid in IDS[:4]}
    for i, values in enumerate(history.values()):
        values[-61] = "10." + "0" * 59 + str(4 - i)
    assert all(variance(values) == 0 for values in history.values())
    result = decide("momentum-low-volatility", history, "1000000")
    assert result["selected"] == list(reversed(IDS[:4]))[:3]
    assert result["scores"] == {sid: str(3 - i) for i, sid in enumerate(IDS[:4])}
