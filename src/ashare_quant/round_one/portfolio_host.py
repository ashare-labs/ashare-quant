"""Native amount rounding/target sizing/cash adjustment, no secondary accounting."""

from decimal import Decimal

from rqalpha.const import MARKET
from rqalpha.interface import ExchangeRate

from ashare_quant.execution_six.contracts import number
from ashare_quant.execution_six.native import Source as SingleSource

from .fee_host import FeeAPI
from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod


class PortfolioAPI(FeeAPI):
    @property
    def total_value(self):
        return Decimal(number(self._host.env.portfolio.total_value))

    def transact(self, value):
        from rqalpha.api import order_shares, order_target_value
        from rqalpha.model.order import MarketOrder

        h, case = self._host, self.case
        expected = (
            self.total_value * Decimal(case.target_weight) if case.target_weight else case.quantity
        )
        if self.clock != "10:00" or h.submissions or value != expected:
            raise RuntimeError("ROUND_ONE_D5_INTENT_MISMATCH")
        h.submissions += 1
        quote = h.read("quote", consumer="strategy")
        h.emit(
            "decision",
            label=case.label,
            api=case.api,
            security=case.security,
            requested=str(value),
            last_price=quote["last"],
            cash_before=str(self.available_cash),
            nav_before=str(self.total_value),
        )
        h.snapshot(case.label + "_before")
        h.emit(
            "order_request",
            api=case.api,
            requested=str(value),
            security=case.security,
            style="market",
            limit_price=None,
        )
        result = (
            order_target_value(case.security, float(value), price_or_style=MarketOrder())
            if case.api == "order_target_value"
            else order_shares(case.security, int(value), price_or_style=MarketOrder())
        )
        if result is not None:
            h.orders[str(result.order_id)] = result
        h.emit(
            "order_return",
            label=case.label,
            returned_none=result is None,
            order=None if result is None else h.project_order(result),
        )
        h.snapshot(case.label + "_after")


class Host(FeeHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.api = PortfolioAPI(self)


class Source(SingleSource):
    def get_exchange_rate(self, trading_date, local, settlement=None):
        if (
            local != MARKET.CN
            or settlement not in {None, MARKET.CN}
            or trading_date != self.host.at.date()
        ):
            raise RuntimeError("ROUND_ONE_CROSS_CURRENCY_UNSUPPORTED")
        self.host.emit(
            "currency_identity",
            currency="CNY",
            factor="1",
            source="same_currency_identity_not_market_quote",
        )
        return ExchangeRate(*([1.0] * 6))


class Mod(FeeMod):
    source_type = Source


def load_mod():
    return Mod()
