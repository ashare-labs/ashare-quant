"""B26 original buy-and-hold intent; all account outcomes belong to native RQAlpha."""

from dataclasses import dataclass
from datetime import date, datetime

from ashare_quant.contracts import SHANGHAI

from .carry_cases import CarryCase


@dataclass(frozen=True)
class DividendCase(CarryCase):
    owner_profile: str = "known_cash_dividend"
    host_module: str = "ashare_quant.round_one.dividend_host"
    research_model: str = "aq.b26-cash-dividend-research.v1"
    query_group: str = "D4"

    @property
    def sessions(self):
        return ("2024-06-13", "2024-06-14")

    @property
    def strategy_path_name(self):
        return "dividend_strategy.py"

    def event_plan(self):
        rows = []
        for session in self.sessions:
            for kind, clock in [
                ("before", "09:00"),
                ("bar" if session == self.sessions[0] else "point", "10:00"),
                ("after", "15:30"),
            ]:
                rows.append(
                    dict(
                        index=len(rows) + 1,
                        kind=kind,
                        at=datetime.fromisoformat(session + "T" + clock)
                        .replace(tzinfo=SHANGHAI)
                        .isoformat(),
                    )
                )
        rows.append(
            dict(index=len(rows) + 1, kind="end_clock", at=self.end_day + "T23:59:59+08:00")
        )
        return tuple(rows)


CASES = {
    "B26": DividendCase(
        "B26",
        "000001.XSHE",
        date(2024, 6, 13),
        "1000000",
        1000,
        "market",
        "1.0",
        end_day="2024-06-14",
        commission="0",
    )
}
