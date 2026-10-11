"""Frozen strategy inputs only. No expected orders, prices, fees or accounts."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ashare_quant.execution_six.contracts import Case


@dataclass(frozen=True)
class FeeCase(Case):
    end_day: str = "2024-03-04"
    buy_orders: int = 1
    commission: str = "0.0003"
    minimum: str = "0"
    sell_tax: str = "0"
    fixed_spread: str = "0"

    def native_costs(self):
        return dict(
            stock_min_commission=float(self.minimum),
            stock_commission_multiplier=float(Decimal(self.commission) / Decimal("0.0008")),
            tax_multiplier=float(Decimal(self.sell_tax) / Decimal("0.0005")),
            pit_tax=False,
        )

    @property
    def strategy_path_name(self):
        return "fee_strategy.py"


CASES = {
    name: FeeCase(
        name,
        "600000.XSHG" if name == "B17" else "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        100 if name == "B15" else 300 if name == "B19" else 10000,
        "market",
        "1.0",
        end_day="2024-03-04" if name in {"B15", "B19"} else "2024-03-05",
        buy_orders=2 if name == "B15" else 1,
        commission="0.0003" if name in {"B14", "B15", "B19"} else "0",
        minimum="5" if name in {"B14", "B15"} else "0",
        sell_tax="0.0005" if name in {"B14", "B16"} else "0",
        fixed_spread="0.02" if name == "B18" else "0",
    )
    for name in ["B14", "B15", "B16", "B17", "B18", "B19"]
}
