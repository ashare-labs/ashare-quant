"""Three single-security D5 intents. No expected fills or adjusted quantities."""

from dataclasses import dataclass
from datetime import date

from .fee_cases import FeeCase


@dataclass(frozen=True)
class PortfolioCase(FeeCase):
    api: str = "order"
    label: str = ""
    target_weight: str | None = None
    auto_switch_order_value: bool = False

    @property
    def strategy_path_name(self):
        return "portfolio_strategy.py"

    @property
    def host_module(self):
        return "ashare_quant.round_one.portfolio_host"


CASES = {
    "B29": PortfolioCase(
        "B29",
        "000001.XSHE",
        date(2024, 3, 4),
        "2000",
        200,
        "market",
        "1.0",
        commission="0.0003",
        minimum="5",
        label="buy_200",
        auto_switch_order_value=True,
    ),
    "B30": PortfolioCase(
        "B30",
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        0,
        "market",
        "1.0",
        commission="0",
        api="order_target_value",
        target_weight="0.2",
        label="target_twenty_percent",
    ),
    "B32": PortfolioCase(
        "B32",
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        250,
        "market",
        "1.0",
        commission="0",
        label="buy_non_round_lot",
    ),
}
