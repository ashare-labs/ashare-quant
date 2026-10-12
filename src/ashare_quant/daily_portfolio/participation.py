"""Exact whole-lot participation budget; no float rounding up to a new lot."""

from fractions import Fraction


def strict_lot_capacity(volume, ratio, lot=100, consumed=0):
    volume, ratio = Fraction(str(volume)), Fraction(str(ratio))
    if volume < 0 or not 0 < ratio <= 1 or lot <= 0 or consumed < 0:
        raise ValueError("INVALID_PARTICIPATION_BUDGET")
    remaining = volume * ratio - Fraction(str(consumed))
    return max(0, int(remaining // lot) * lot)
