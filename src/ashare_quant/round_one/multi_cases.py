from dataclasses import dataclass
from datetime import date

from .fee_cases import FeeCase


@dataclass(frozen=True)
class MultiCase(FeeCase):
    securities: tuple = ("000001.XSHE", "600000.XSHG")
    order_intents: tuple = (
        ("000001.XSHE", 100, "first_ping_an"),
        ("600000.XSHG", 300, "second_pudong"),
    )
    owner_profile: str = "priority2"
    auto_switch_order_value: bool = True
    host_module: str = "ashare_quant.round_one.multi_host"

    @property
    def strategy_path_name(self):
        return "multi_strategy.py"


CASES = {
    "B31": MultiCase(
        "B31",
        "000001.XSHE",
        date(2024, 3, 4),
        "3000",
        0,
        "market",
        "1.0",
        buy_orders=2,
        commission="0",
        minimum="0",
    )
}
