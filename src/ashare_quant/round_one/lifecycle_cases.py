"""Bounded original D6 single-session inputs; no expected results."""

from dataclasses import dataclass
from datetime import date

from .fee_cases import FeeCase


@dataclass(frozen=True)
class LifecycleCase(FeeCase):
    host_module: str = "ashare_quant.round_one.lifecycle_host"
    query_group: str | None = None

    @property
    def strategy_path_name(self):
        return "lifecycle_strategy.py"

    def event_plan(self):
        clocks = [("before", "09:00")]
        if self.case_id == "B36":
            clocks += [("callback", "09:30"), ("bar", "10:00")]
        else:
            clocks += [("bar", "10:00"), ("bar", "10:01")]
        clocks += [("after", "15:30"), ("end_clock", "23:59:59")]
        return tuple(
            dict(index=i, kind=k, at=self.at(t).isoformat()) for i, (k, t) in enumerate(clocks, 1)
        )


CASES = {
    c: LifecycleCase(
        c,
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        100 if c == "B37" else 0,
        "market",
        "1.0",
        commission="0",
        minimum="0",
        buy_orders=1 if c == "B37" else 0,
        query_group="D6" if c in {"B38", "B39"} else None,
    )
    for c in ["B36", "B37", "B38", "B39"]
}
