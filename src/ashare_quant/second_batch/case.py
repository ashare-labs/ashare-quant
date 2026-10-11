"""Original source configuration, without reference results."""

from dataclasses import dataclass
from datetime import date, datetime

from ashare_quant.contracts import SHANGHAI


@dataclass(frozen=True)
class Case:
    config: dict
    script: str
    policy: str

    @property
    def case_id(self):
        return self.config["case_id"]

    @property
    def security(self):
        return self.config["symbol"]

    @property
    def securities(self):
        return [self.security] + (
            [self.config["second_symbol"]] if "second_symbol" in self.config else []
        )

    @property
    def day(self):
        return date.fromisoformat(self.config["start_date"])

    @property
    def initial_cash(self):
        return self.config["initial_cash"]

    @property
    def volume_ratio(self):
        return self.config["order_volume_ratio"]

    @property
    def end_day(self):
        return self.config["end_date"]

    def at(self, clock):
        return datetime.fromisoformat(str(self.day) + "T" + clock).replace(tzinfo=SHANGHAI)
