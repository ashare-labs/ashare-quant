"""Point price/capacity seams only; all orders, fills and accounting remain native."""

from datetime import time

from rqalpha.const import INSTRUMENT_TYPE
from rqalpha.core.events import EVENT, Event
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderNotMatchable

from ashare_quant.execution_six.contracts import owner_price, owner_volume
from ashare_quant.execution_six.native import Board, Source

from .fee_host import Host as FeeHost
from .fee_host import Mod as FeeMod


class PointSource(Source):
    def get_bar(self, *args, **kwargs):
        raise RuntimeError("POINT_MODEL_HAS_NO_OHLC_BAR")


class PointBoard(Board):
    def get_last_price(self, sid):
        self.host.security(sid)
        if self.host.at.time() >= time(15, 30):
            return super().get_last_price(sid)
        if self.host.point_value is not None:
            return owner_price(self.host.point_value["price"])
        return owner_price(self.host.quote_model()["last"])

    def get_limit_up(self, sid):
        self.host.security(sid)
        return owner_price(self.host.quote_model()["upper_limit"])

    def get_limit_down(self, sid):
        self.host.security(sid)
        return owner_price(self.host.quote_model()["lower_limit"])


class PointMatcher(DefaultBarMatcher):
    def __init__(self, host, config):
        self.host = host
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        if not self.host.point_pulse or open_auction:
            raise OrderNotMatchable("AQ declared point model: no matching outside point pulse")
        return owner_price(self.host.point_value["price"])

    def _get_bar_volume(self, order, open_auction=False):
        if not self.host.point_pulse or open_auction:
            raise RuntimeError("POINT_CAPACITY_OUTSIDE_PULSE")
        return owner_volume(self.host.point_value["volume"])


class Host(FeeHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.point_pulse, self.point_value, self.observed_quote = False, None, None

    def read(self, operation, consumer="engine", **parameters):
        if operation == "quote" and consumer == "engine" and self.observed_quote is not None:
            return self.quote_model()
        value = super().read(operation, consumer=consumer, **parameters)
        if operation == "quote" and self.at == self.case.at("12:00"):
            self.observed_quote = value
        return value

    def quote_model(self):
        if self.observed_quote is None or self.at < self.case.at("12:00"):
            raise RuntimeError("POINT_RESERVATION_REQUIRES_PRIOR_OBSERVED_QUOTE")
        self.emit(
            "engine_quote_model",
            source_at=self.case.at("12:00").isoformat(),
            status_limits="held_from_observed_12_quote",
            current_quote_certified=False,
            strategy_visible=False,
        )
        return self.observed_quote

    def user_event(self, event):
        if event.aq_kind != "point":
            return super().user_event(event)
        self.point_value = self.read("point")
        self.emit(
            "point_model_event",
            **self.point_value,
            strategy_visible=False,
            historical_pit=False,
            matching=self.api.clock == "13:01",
            matching_phase="after_user_callback",
        )
        self.env.portfolio.stock_account._on_bar(event)
        super().user_event(event)
        if self.api.clock == "13:01":
            self.point_pulse = True
            try:
                self.env.broker.on_bar(Event(EVENT.BAR))
            finally:
                self.point_pulse = False
            self.env.portfolio.stock_account._on_bar(event)

    def complete(self):
        result = super().complete()
        result.update(
            execution_model="b20.retrospective_point_price.v1",
            no_ohlc_synthesis=True,
            strategy_point_access=False,
            independently_sourced_point_execution_not_jq_quote_equivalence=True,
        )
        return result


class Mod(FeeMod):
    source_type = PointSource

    def start_up(self, env, config):
        super().start_up(env, config)
        env.set_price_board(PointBoard(self.host))

    def ready(self, event):
        h = self.host
        h.env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, PointMatcher(h, h.env.config.mod.sys_simulation)
        )


def load_mod():
    return Mod()
