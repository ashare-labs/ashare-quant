"""Saved file paths identify strategy slots; source revisions identify individual runs."""

from typing import Annotated

from pydantic import AfterValidator, Field

from ashare_quant.strategies.files import parts


def strategy_path(value):
    parts(value, file=True)
    return value


StrategyID = Annotated[str, Field(min_length=1, max_length=1024), AfterValidator(strategy_path)]
