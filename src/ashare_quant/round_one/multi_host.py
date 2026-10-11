"""Per-security native data routing, one native portfolio and shared cash account."""

from dataclasses import replace

from rqalpha.const import INSTRUMENT_TYPE
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderCancelled
from rqalpha.model.instrument import Instrument

from ashare_quant.execution_six.contracts import owner_volume
from ashare_quant.execution_six.native import Board as SingleBoard
from ashare_quant.execution_six.native import ScopedInstrument
from ashare_quant.execution_six.native import Source as SingleSource

from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod
from .portfolio_host import PortfolioAPI
from .portfolio_host import Source as CurrencySource


class BoundHost:
    def __init__(self, host, sid):
        self.host, self.sid = host, sid
        self.case = replace(host.case, security=sid)

    def __getattr__(self, name):
        return getattr(self.host, name)

    def read(self, operation, **kwargs):
        return self.host.read(operation, security=self.sid, **kwargs)


class MultiInstrument(ScopedInstrument):
    def __init__(self, host):
        self.host = host
        Instrument.__init__(
            self,
            {
                "order_book_id": host.case.security,
                "symbol": host.case.security,
                "type": "CS",
                "exchange": host.case.security.rsplit(".", 1)[1],
                "round_lot": 100,
                "board_type": "MainBoard",
                "market_tplus": 1,
            },
        )


class Source(CurrencySource):
    def __init__(self, host):
        self.host = host
        self.sources = {sid: SingleSource(BoundHost(host, sid)) for sid in host.case.securities}
        for sid, source in self.sources.items():
            source.instrument = MultiInstrument(source.host)

    def get_instruments(self, id_or_syms=None, types=None):
        ids = list(self.sources) if id_or_syms is None else id_or_syms
        for sid in ids:
            self.host.security(sid)
        return (
            [self.sources[sid].instrument for sid in ids]
            if types is None or INSTRUMENT_TYPE.CS in types
            else []
        )

    def get_bar(self, instrument, dt, frequency):
        return self.sources[instrument.order_book_id].get_bar(instrument, dt, frequency)

    def history_bars(self, instrument, *args, **kwargs):
        return self.sources[instrument.order_book_id].history_bars(instrument, *args, **kwargs)

    def is_suspended(self, sid, dates):
        return self.sources[sid].is_suspended(sid, dates)

    def is_st_stock(self, sid, dates):
        return self.sources[sid].is_st_stock(sid, dates)


class Board(SingleBoard):
    def __init__(self, host):
        self.host = host
        self.boards = {sid: SingleBoard(BoundHost(host, sid)) for sid in host.case.securities}

    def get_last_price(self, sid):
        self.host.security(sid)
        return self.boards[sid].get_last_price(sid)

    def get_limit_up(self, sid):
        self.host.security(sid)
        return self.boards[sid].get_limit_up(sid)

    def get_limit_down(self, sid):
        self.host.security(sid)
        return self.boards[sid].get_limit_down(sid)


class Matcher(DefaultBarMatcher):
    def __init__(self, host, config):
        self.host = host
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        if self.host.at.strftime("%H:%M") != "10:00" or open_auction:
            raise RuntimeError("MULTI_MATCH_OUTSIDE_FROZEN_CLOCK")
        bar = self.host.read("bar", security=order.order_book_id)
        if owner_volume(bar["volume"]) == 0:
            raise OrderCancelled("AD observed zero minute volume")
        return super()._get_deal_price(order, instrument, open_auction)


class MultiAPI(PortfolioAPI):
    def transact(self, sid, quantity, label):
        from rqalpha.api import order_shares
        from rqalpha.model.order import MarketOrder

        h = self._host
        if (
            self.clock != "10:00"
            or h.submissions >= len(self.case.order_intents)
            or (sid, quantity, label) != self.case.order_intents[h.submissions]
        ):
            raise RuntimeError("MULTI_ORDER_INTENT_MISMATCH")
        h.submissions += 1
        quote = h.read("quote", consumer="strategy", security=sid)
        h.emit(
            "decision",
            api="order",
            security=sid,
            requested=str(quantity),
            label=label,
            cash_before=str(self.available_cash),
            nav_before=str(self.total_value),
            last_price=quote["last"],
        )
        h.snapshot(label + "_before")
        h.emit(
            "order_request",
            api="order",
            security=sid,
            requested=str(quantity),
            style="market",
            limit_price=None,
        )
        result = order_shares(sid, quantity, price_or_style=MarketOrder())
        if result is not None:
            h.orders[str(result.order_id)] = result
        h.emit(
            "order_return",
            label=label,
            returned_none=result is None,
            order=None if result is None else h.project_order(result),
        )
        h.snapshot(label + "_after")


class Host(FeeHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.api = MultiAPI(self)

    def security(self, sid):
        if self.closed or sid not in self.case.securities:
            raise RuntimeError("MULTI_SECURITY_OUT_OF_SCOPE_OR_CLOSED")


class Mod(FeeMod):
    source_type = Source

    def start_up(self, env, config):
        super().start_up(env, config)
        env.set_price_board(Board(self.host))

    def ready(self, event):
        self.host.env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, Matcher(self.host, self.host.env.config.mod.sys_simulation)
        )


def load_mod():
    return Mod()
