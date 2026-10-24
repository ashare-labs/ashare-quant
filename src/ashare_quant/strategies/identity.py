"""Legacy file paths and stable project IDs identify strategy slots."""

import re
from typing import Annotated

from pydantic import AfterValidator, Field

from ashare_quant.strategies.files import parts

PROJECT_ID_PATTERN = r"^aqproj-[0-9a-f]{32}$"


def is_project_id(value):
    return isinstance(value, str) and re.fullmatch(PROJECT_ID_PATTERN, value) is not None


def strategy_path(value):
    if not is_project_id(value):
        parts(value, file=True)
    return value


StrategyID = Annotated[str, Field(min_length=1, max_length=1024), AfterValidator(strategy_path)]
