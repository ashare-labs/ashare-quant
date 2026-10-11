"""B35 no-order state persistence over two explicitly observed session labels."""

from dataclasses import dataclass
from datetime import date, datetime

from ashare_quant.contracts import SHANGHAI

from .fee_cases import FeeCase


@dataclass(frozen=True)
class StateCase(FeeCase):
    host_module: str = "ashare_quant.round_one.state_host"
    owner_profile: str = "empty_sessions"
    securities: tuple = ("000001.XSHE",)

    @property
    def strategy_path_name(self):
        return "state_strategy.py"

    def event_plan(self):
        rows = []
        for day in [str(self.day), self.end_day]:
            for kind, at in [("before", "09:00"), ("callback", "10:00"), ("after", "15:30")]:
                rows.append(
                    dict(
                        index=len(rows) + 1,
                        kind=kind,
                        at=datetime.fromisoformat(day + "T" + at)
                        .replace(tzinfo=SHANGHAI)
                        .isoformat(),
                    )
                )
        rows.append(
            dict(index=len(rows) + 1, kind="end_clock", at=self.end_day + "T23:59:59+08:00")
        )
        return tuple(rows)


CASES = {
    "B35": StateCase(
        "B35",
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        0,
        "market",
        "1.0",
        end_day="2024-03-05",
        buy_orders=0,
        commission="0",
    )
}
