"""Whole-lot targets for the ordinary-stock model, independent of sample sizes."""

from typing import Annotated

from pydantic import Field

# The native engine represents quantities using binary64 in some projections.
MAX_EXACT_SHARES = 2**53 - 1
TargetQuantity = Annotated[
    int, Field(strict=True, ge=0, le=MAX_EXACT_SHARES, multiple_of=100)
]


def valid_target(value):
    return type(value) is int and 0 <= value <= MAX_EXACT_SHARES and value % 100 == 0
