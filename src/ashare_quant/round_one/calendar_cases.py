"""B21 frozen empty-account strategy; execution dates come from the owner calendar."""

from dataclasses import dataclass
from datetime import date

from .state_cases import StateCase


@dataclass(frozen=True)
class CalendarCase(StateCase):
    host_module: str = "ashare_quant.round_one.calendar_host"
    owner_profile: str = "bounded_calendar"
    query_group: str = "calendar"

    @property
    def strategy_path_name(self):
        return "calendar_strategy.py"

    def event_plan(self, metadata):
        rows = []
        for day in metadata["calendar"]:
            for kind, at in [("before", "09:00"), ("callback", "10:00"), ("after", "15:30")]:
                rows.append(dict(index=len(rows) + 1, kind=kind, at=day + "T" + at + ":00+08:00"))
        rows.append(
            dict(index=len(rows) + 1, kind="end_clock", at=self.end_day + "T23:59:59+08:00")
        )
        return tuple(rows)


CASES = {
    "B21": CalendarCase(
        "B21",
        "000001.XSHE",
        date(2024, 4, 3),
        "1000000",
        0,
        "market",
        "1.0",
        end_day="2024-04-08",
        buy_orders=0,
        commission="0",
    )
}
