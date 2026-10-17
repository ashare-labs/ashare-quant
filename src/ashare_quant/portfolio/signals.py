"""Pure, previous-session-only portfolio signals for the frozen acceptance questions."""

from datetime import date
from decimal import ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction


def scheduled(strategy, day, previous, first):
    if first or strategy in {"trend", "daily-equal-weight"}:
        return True
    today, prior = date.fromisoformat(day), date.fromisoformat(previous)
    if strategy in {"equal-weight", "low-volatility"}:
        return (today.year, today.month) != (prior.year, prior.month)
    if strategy in {"momentum", "weekly-reversal", "momentum-low-volatility"}:
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
    if strategy in {"equal-weight", "daily-equal-weight"}:
        chosen, denominator = ids, len(ids)
    elif strategy == "weekly-reversal":
        if len(ids) < 2:
            raise ValueError("REVERSAL_REQUIRES_TWO_SECURITIES")
        scores = {s: prices[s][-1] / prices[s][-6] - 1 for s in ids}
        chosen, denominator = sorted(ids, key=lambda s: (scores[s], s))[:2], 2
    elif strategy == "momentum":
        if len(ids) < 3:
            raise ValueError("MOMENTUM_REQUIRES_THREE_SECURITIES")
        scores = {s: prices[s][-1] / prices[s][-61] - 1 for s in ids}
        chosen, denominator = sorted(ids, key=lambda s: (-scores[s], s))[:3], 3
    elif strategy == "trend":
        scores = {s: sum(prices[s]) / 120 for s in ids}
        chosen = [s for s in ids if prices[s][-1] > scores[s]]
        denominator = len(ids)
    elif strategy in {"low-volatility", "momentum-low-volatility"}:
        if len(ids) < 3:
            raise ValueError("RANK_PORTFOLIO_REQUIRES_THREE_SECURITIES")
        variances = {}
        for sid in ids:
            # Decimal construction preserves the input, but Decimal division and
            # accumulation round. Exact factor keys keep equal return multisets
            # tied regardless of their order, without merging nearby values.
            tail = [Fraction(p) for p in prices[sid][-21:]]
            returns = [b / a - 1 for a, b in zip(tail[:-1], tail[1:], strict=True)]
            mean = sum(returns) / 20
            variances[sid] = sum((r - mean) ** 2 for r in returns) / 20
        if strategy == "low-volatility":
            scores = variances
        else:
            momentum = {s: Fraction(prices[s][-1]) / Fraction(prices[s][-61]) - 1 for s in ids}
            # Equal values receive the same competition rank. Security IDs only
            # break the final portfolio tie, not the numeric factor ranks.
            scores = {
                s: Decimal(
                    sum(v > momentum[s] for v in momentum.values())
                    + sum(v < variances[s] for v in variances.values())
                )
                for s in ids
            }
        chosen, denominator = sorted(ids, key=lambda s: (scores[s], s))[:3], 3
        if strategy == "low-volatility":
            # Preserve the decimal-string output contract only after selection.
            scores = {s: Decimal(v.numerator) / Decimal(v.denominator) for s, v in scores.items()}
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
