"""Synthetic inputs only, kept separate from native accounting and real A数达."""

import numpy as np

from rqprobe.fixture import DTYPE, FrozenFixture, SyntheticProvider, aware


class ScenarioProvider(SyntheticProvider):
    def __init__(self, scenario):
        super().__init__(actions=scenario == "corporate_action")
        self.scenario = scenario
        prices = {"buy_hold": 11, "restart": 11, "t_plus_one": 11,
                  "cross_day": 9, "corporate_action": 9}
        for freq, rows in self.rows.items():
            for row in rows:
                price = prices.get(scenario, 10) if row["datetime"] >= 20240103000000 else 10
                # These are expressly declared synthetic data facts, not inferred market data.
                for name, value in {"open": price - .5 if freq == "1d" else price,
                                    "high": price + .5, "low": price - .5, "close": price,
                                    "total_turnover": price * row["volume"],
                                    "limit_up": price * 1.1, "limit_down": price * .9}.items():
                    row[name] = value

    def at(self, as_of):
        return ScenarioFrame(self, aware(as_of))


class ScenarioFrame(FrozenFixture):
    def bars(self, sid, frequency, end, count=None, adjust_type="none", adjust_orig=None):
        rows = super().bars(sid, frequency, end, count, "none", adjust_orig)
        if adjust_type not in {"none", "pre"}:
            raise ValueError("SYNTHETIC_ADJUSTMENT_UNSUPPORTED")
        # Declared dividend-only synthetic adjustment; no split in this seven-case suite.
        if self.parent.actions and adjust_type == "pre" and aware(adjust_orig or self.as_of).year >= 2024:
            for row in rows:
                if row["datetime"] < 20240103000000 and self.as_of.isoformat() >= "2024-01-03":
                    for field in ["open", "high", "low", "close"]:
                        row[field] -= 1
        return np.array(rows, dtype=DTYPE)

    def corporate_actions(self, kind):
        return None if kind == "split" else super().corporate_actions(kind)
