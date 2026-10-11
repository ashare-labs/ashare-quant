"""Six fixed short-window intents; no reference output is used as an input."""

from dataclasses import asdict, dataclass
from datetime import date, datetime

from ashare_quant.contracts import SHANGHAI

from .cash_account import MODEL
from .fee_cases import CASES as FEES
from .fee_cases import FeeCase


@dataclass(frozen=True)
class CarryCase(FeeCase):
    owner_profile: str = "bounded_carry"
    host_module: str = "ashare_quant.round_one.carry_host"
    cash_model: str = MODEL
    research_model: str = "aq.six-carry-research.v1"

    @property
    def sessions(self):
        return (
            ("2024-03-04", "2024-03-05", "2024-03-06")
            if self.case_id == "B22"
            else ("2024-03-04", "2024-03-05")
        )

    @property
    def strategy_path_name(self):
        return "carry_strategy.py"

    def event_plan(self):
        rows = []
        for session in self.sessions:
            kind = "callback" if session == "2024-03-06" else "bar"
            for event, clock in [
                ("before", "09:00"),
                (kind, "10:00"),
                (kind, "10:01"),
                ("after", "15:30"),
            ]:
                rows.append(
                    dict(
                        index=len(rows) + 1,
                        kind=event,
                        at=datetime.fromisoformat(session + "T" + clock)
                        .replace(tzinfo=SHANGHAI)
                        .isoformat(),
                    )
                )
        rows.append(
            dict(index=len(rows) + 1, kind="end_clock", at=self.end_day + "T23:59:59+08:00")
        )
        return tuple(rows)


CASES = {name: CarryCase(**asdict(FEES[name])) for name in ("B14", "B16", "B17", "B18")}
for name, quantity, end in [("B22", 10000, "2024-03-06"), ("B33", 1000, "2024-03-05")]:
    CASES[name] = CarryCase(
        name,
        "000001.XSHE",
        date(2024, 3, 4),
        "1000000",
        quantity,
        "market",
        "1.0",
        end_day=end,
        commission="0",
    )
