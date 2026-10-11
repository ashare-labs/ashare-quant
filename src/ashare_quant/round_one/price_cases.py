from dataclasses import dataclass
from datetime import date

from .fee_cases import FeeCase


@dataclass(frozen=True)
class PriceCase(FeeCase):
    query_group: str = "D4"
    host_module: str = "ashare_quant.round_one.price_host"

    @property
    def strategy_path_name(self):
        return "price_strategy.py"


CASES = {
    "B24": PriceCase(
        "B24",
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        100,
        "market",
        "1.0",
        commission="0",
        minimum="0",
    )
}
