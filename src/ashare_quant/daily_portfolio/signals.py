"""Pure, previous-session-only portfolio signals for the frozen acceptance questions."""

from datetime import date
from decimal import ROUND_FLOOR, Decimal, localcontext


def scheduled(strategy, day, previous, first):
    if first or strategy == "trend":
        return True
    today, prior = date.fromisoformat(day), date.fromisoformat(previous)
    if strategy == "equal-weight":
        return (today.year, today.month) != (prior.year, prior.month)
    if strategy == "momentum":
        return today.isocalendar()[:2] != prior.isocalendar()[:2]
    raise ValueError("UNKNOWN_STRATEGY")


def decide(strategy, history, equity):
    # Native dependencies may change the process decimal context while converting
    # small orders. Fixed formula precision must not depend on the preceding fill.
    with localcontext(prec=50):
        return _decide(strategy, history, equity)


def _decide(strategy, history, equity):
    """history maps frozen securities to exactly 120 closes ending on P.

    No current bars, provider client, native account or mutable globals enter here.
    Zero targets are explicit; ties and the equality-to-SMA cash boundary are stable.
    """
    ids = sorted(history)
    if not ids or any(len(v) != 120 for v in history.values()):
        raise ValueError("EXACT_120_SESSION_HISTORY_REQUIRED")
    prices = {s: tuple(Decimal(str(v)) for v in history[s]) for s in ids}
    if any(not p.is_finite() or p <= 0 for values in prices.values() for p in values):
        raise ValueError("MISSING_OR_INVALID_SIGNAL_PRICE")
    equity = Decimal(str(equity))
    if not equity.is_finite() or equity < 0:
        raise ValueError("INVALID_PRIOR_EQUITY")
    scores = {}
    if strategy == "equal-weight":
        chosen, denominator = ids, len(ids)
    elif strategy == "momentum":
        if len(ids) < 3:
            raise ValueError("MOMENTUM_REQUIRES_THREE_SECURITIES")
        scores = {s: prices[s][-1] / prices[s][-61] - 1 for s in ids}
        chosen, denominator = sorted(ids, key=lambda s: (-scores[s], s))[:3], 3
    elif strategy == "trend":
        scores = {s: sum(prices[s]) / 120 for s in ids}
        chosen = [s for s in ids if prices[s][-1] > scores[s]]
        denominator = len(ids)
    else:
        raise ValueError("UNKNOWN_STRATEGY")
    weights = {s: Decimal("0.98") / denominator if s in chosen else Decimal(0) for s in ids}
    targets = {
        s: int((equity * weights[s] / prices[s][-1] / 100).to_integral_value(ROUND_FLOOR)) * 100
        for s in ids
    }
    return {
        "selected": chosen,
        "scores": {s: str(v) for s, v in scores.items()},
        "weights": {s: str(v) for s, v in weights.items()},
        "targets": targets,
    }


def order_plan(targets, quantities):
    deltas = {s: targets[s] - quantities.get(s, 0) for s in sorted(targets)}
    return [(s, q) for s, q in deltas.items() if q < 0] + [
        (s, q) for s, q in deltas.items() if q > 0
    ]
