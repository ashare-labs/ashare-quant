"""Small explicit execution policies; never import reference answers."""

from decimal import ROUND_FLOOR, Decimal


def sell_quantity(requested, sellable):
    """Clamp a raw stock sell intent to the native currently closable quantity."""
    if type(requested) is not int or type(sellable) is not int or sellable < 0:
        raise ValueError("SECOND_INVALID_SELL_QUANTITY")
    return max(requested, -sellable) if requested < 0 else requested


def resting_capacity(volume, ratio, already_used=0):
    """Whole shares for a resting order; no new 100-share order-lot rounding."""
    v, r = Decimal(str(volume)), Decimal(str(ratio))
    if not v.is_finite() or not r.is_finite() or v < 0 or not 0 < r <= 1:
        raise ValueError("SECOND_INVALID_MINUTE_CAPACITY")
    if type(already_used) is not int or already_used < 0:
        raise ValueError("SECOND_INVALID_USED_CAPACITY")
    return max(0, int((v * r).to_integral_value(rounding=ROUND_FLOOR)) - already_used)
