"""B20 frozen input clocks with an explicitly retrospective point-price model."""

from dataclasses import dataclass
from datetime import date

from .fee_cases import FeeCase


@dataclass(frozen=True)
class PointCase(FeeCase):
    host_module: str = "ashare_quant.round_one.point_host"
    owner_profile: str = "lunch_point"

    @property
    def strategy_path_name(self):
        return "point_strategy.py"

    def event_plan(self):
        clocks = [("before", "09:00")]
        clocks += [
            ("point" if t in {"13:01", "15:00"} else "callback", t)
            for t in ["11:29", "11:30", "12:00", "12:59", "13:00", "13:01", "15:00"]
        ]
        clocks += [("after", "15:30"), ("end_clock", "23:59:59")]
        return tuple(
            dict(index=i, kind=k, at=self.at(t).isoformat()) for i, (k, t) in enumerate(clocks, 1)
        )


CASES = {
    "B20": PointCase(
        "B20",
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        100,
        "market",
        "1.0",
        submit_at="12:00",
        commission="0",
    )
}
