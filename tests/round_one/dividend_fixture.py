"""Synthetic account/market only; tests production fact-to-native schema translation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ashare_quant.round_one.carry_contract import dividend_records
from rqprobe.fixture import FrozenFixture, SyntheticProvider

FACT = dict(
    security="600000.XSHG",
    schema="ad.document-cash-dividend-fact.v1",
    event_type="cash_dividend",
    currency="CNY",
    historical_pit=False,
    strategy_visible=False,
    announcement_date="2023-12-29",
    record_date="2024-01-02",
    ex_date="2024-01-03",
    payment_date="2024-01-04",
    gross_cash_per_10_shares="7.0",
    bonus_share_ratio="0",
    capitalization_share_ratio="0",
)


class Frame(FrozenFixture):
    def corporate_actions(self, kind):
        if kind == "split":
            return None
        return dividend_records(
            FACT, security=FACT["security"], as_of=self.as_of, role="engine_known_event_research"
        )


class Provider(SyntheticProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(actions=False)

    def at(self, as_of):
        if isinstance(as_of, datetime) and as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return Frame(self, as_of)


def main():
    import rqprobe.launcher as launcher
    import rqprobe.mod as mod

    launcher.SyntheticProvider = mod.SyntheticProvider = Provider
    launcher.run()
