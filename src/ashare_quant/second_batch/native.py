"""Sparse quote execution and selected native lifecycle events.

No constructed OHLC bars. Price and capacity use separately declared AD reads;
DefaultBarMatcher still creates trades and native Account applies them.
"""

import sys
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, MARKET, ORDER_TYPE, SIDE, TRADING_CALENDAR_TYPE
from rqalpha.core.strategy_universe import StrategyUniverse
from rqalpha.interface import AbstractDataSource, AbstractPriceBoard, ExchangeRate
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderNotMatchable
from rqalpha.model.instrument import Instrument as BaseInstrument
from rqalpha.portfolio.capital_gains_tax import CapitalGainsTaxMixin

from ashare_quant.execution_six.native import Proxy as BaseProxy


class Instrument(BaseInstrument):
    def __init__(self, h, sid):
        self.h = h
        super().__init__(
            dict(
                order_book_id=sid,
                symbol=sid,
                type="CS",
                exchange=sid.split(".")[1],
                round_lot=100,
                board_type="MainBoard",
                market_tplus=1,
            )
        )

    def active_at(self, dt):
        return self.listed_at(dt)

    def listed_at(self, dt):
        value = str(dt.date() if hasattr(dt, "date") else dt)
        if value not in self.h.metadata["calendar"]:
            raise RuntimeError("SECOND_LIFECYCLE_OUTSIDE_MODEL")
        return True

    def de_listed_at(self, dt):
        if dt is None and sys._getframe(1).f_code is StockPosition.settlement.__code__:
            return False  # bounded terminal holding, no fabricated next session
        self.listed_at(dt)
        return False

    @property
    def listed_date(self):
        raise RuntimeError("SECOND_ABSOLUTE_LISTING_DATE_UNKNOWN")

    @property
    def de_listed_date(self):
        if sys._getframe(1).f_code is not StrategyUniverse._clear_de_listed.__code__:
            raise RuntimeError("SECOND_ABSOLUTE_DELISTING_DATE_UNKNOWN")
        return datetime.combine(date.fromisoformat(self.h.case.end_day) + timedelta(days=1), time())


class Source(AbstractDataSource):
    def __init__(self, h):
        self.host = h
        self.instruments = {s: Instrument(h, s) for s in h.case.securities}

    def get_instruments(self, id_or_syms=None, types=None):
        ids = self.host.case.securities if id_or_syms is None else id_or_syms
        for s in ids:
            self.host.security(s)
        return (
            [self.instruments[s] for s in ids]
            if types is None or INSTRUMENT_TYPE.CS in types
            else []
        )

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: pd.DatetimeIndex(self.host.metadata["calendar"])}

    def available_data_range(self, frequency):
        return self.host.case.day, date.fromisoformat(self.host.case.end_day)

    def get_bar(self, *args, **kwargs):
        raise RuntimeError("SECOND_NO_FABRICATED_BAR")

    def history_bars(self, instrument, bar_count, frequency, fields, dt, **kwargs):
        prior = self.host.previous[str(self.host.at.date())]
        if (
            bar_count != 1
            or frequency != "1d"
            or fields != "close"
            or str(dt.date() if hasattr(dt, "date") else dt) != prior
        ):
            raise RuntimeError("SECOND_HISTORY_SCOPE")
        return np.array([self.host.daily(instrument.order_book_id, prior, "close")])

    def is_suspended(self, sid, dates):
        return [self.host.quote(sid)["paused"] for _ in dates]

    def is_st_stock(self, sid, dates):
        raise RuntimeError("SECOND_ST_UNKNOWN")

    def get_dividend(self, instrument):
        if self.host.action is None:
            return None
        f = self.host.action["rows"][0]
        v = f["facts"]
        self.host.emit(
            "native_action_input",
            category="dividend",
            owner_evidence=self.host.action,
            payment_date_model=(
                "issuer implementation schedule, 09:00 native posting research phase"
                if self.host.case.policy == "integrated"
                else "ex_date_assumption; not issuer-certified"
            ),
        )

        def dayint(s):
            return int(s.replace("-", ""))

        return np.array(
            [
                (
                    dayint(v["record_date"]),
                    dayint(f["ex_date"]),
                    dayint(
                        v["cash_payment_date"]
                        if self.host.case.policy == "integrated"
                        else f["ex_date"]
                    ),
                    float(v["gross_cash_per_10"]),
                    10,
                )
            ],
            dtype=[
                ("book_closure_date", "i8"),
                ("ex_dividend_date", "i8"),
                ("payable_date", "i8"),
                ("dividend_cash_before_tax", "f8"),
                ("round_lot", "i8"),
            ],
        )

    def get_split(self, instrument):
        if self.host.action is None:
            return None
        f = self.host.action["rows"][0]
        v = f["facts"]
        if self.host.case.policy == "integrated" and not (
            v["shares_credit_date"] == v["new_shares_tradable_date"] == f["ex_date"]
        ):
            raise RuntimeError("SECOND_SEPARATE_SHARE_CREDIT_TRADABILITY_UNSUPPORTED")
        self.host.emit(
            "native_action_input",
            category="capitalization",
            owner_evidence=self.host.action,
            tradable_date_model=(
                "issuer unlimited-share tradable date"
                if self.host.case.policy == "integrated"
                else "ex_date_assumption; not issuer-certified"
            ),
        )
        return np.array(
            [
                (
                    int(f["ex_date"].replace("-", "")) * 1000000,
                    10 + float(v["capitalization_shares_per_10"]),
                    10,
                )
            ],
            dtype=[
                ("ex_date", "i8"),
                ("split_coefficient_to", "f8"),
                ("split_coefficient_from", "f8"),
            ],
        )

    def get_share_transformation(self, sid):
        return None

    def current_snapshot(self, *a, **k):
        raise RuntimeError("SECOND_AGGREGATE_SNAPSHOT_UNSUPPORTED")

    def get_exchange_rate(self, trading_date, local, settlement=None):
        if local != MARKET.CN or settlement not in {None, MARKET.CN}:
            raise RuntimeError("SECOND_CURRENCY_SCOPE")
        return ExchangeRate(*([1.0] * 6))


class Proxy(BaseProxy):
    def get_previous_trading_date(self, dt, n=1, *args, **kwargs):
        h = self._data_source.host
        key = str(dt.date() if hasattr(dt, "date") else dt)
        if n != 1 or key not in h.previous:
            raise RuntimeError("SECOND_PREVIOUS_CALENDAR_UNKNOWN")
        return datetime.fromisoformat(h.previous[key])

    def get_next_trading_date(self, dt, n=1, *args, **kwargs):
        h = self._data_source.host
        key = str(dt.date() if hasattr(dt, "date") else dt)
        found = [d for d, p in h.previous.items() if p == key]
        if n == 1 and len(found) == 1:
            return datetime.fromisoformat(found[0])
        caller = sys._getframe(1)
        if n == 1 and key == h.case.end_day:
            if caller.f_code is StockPosition.settlement.__code__:
                return None
            if caller.f_code is CapitalGainsTaxMixin._is_end_of_month.__code__:
                # Only a period scalar for zero-tax accounting, not a next session date.
                if h.env.config.base.capital_gain_tax_rate != 0:
                    raise RuntimeError("SECOND_TAX_MODEL")
                return SimpleNamespace(month=h.at.month)
        raise RuntimeError("SECOND_NEXT_CALENDAR_UNKNOWN")


class Board(AbstractPriceBoard):
    def __init__(self, h):
        self.h = h

    def get_last_price(self, sid):
        h = self.h
        h.security(sid)
        if h.at.strftime("%H:%M") == "09:00":
            return h.daily(sid, h.previous[str(h.at.date())], "close")
        if h.at.time() >= time(15, 30):
            return h.daily(sid, str(h.at.date()), "close")
        return h.quote(sid)["last_price"]

    def get_limit_up(self, sid):
        return self.h.quote(sid)["high_limit"]

    def get_limit_down(self, sid):
        return self.h.quote(sid)["low_limit"]


class Matcher(DefaultBarMatcher):
    def __init__(self, h, config):
        self.h = h
        super().__init__(h.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        if open_auction:
            raise RuntimeError("SECOND_AUCTION_UNSUPPORTED")
        if self.h.at.time() < time(9, 30):
            raise OrderNotMatchable("pre-open")
        if (
            self.h.case.policy == "integrated"
            and order.type == ORDER_TYPE.LIMIT
            and order.side == SIDE.BUY
            and order.datetime < self.h.at.replace(tzinfo=None)
        ):
            bar = self._resting_bar(order)
            if float(order.price) <= float(bar["values"]["low"]):
                raise OrderNotMatchable("Resting buy limit does not cross observed bar low")
            self.h.emit(
                "resting_limit_price_policy",
                price=order.price,
                price_origin="order limit under documented JQ bar model, not observed market price",
                bar_low=bar["values"]["low"],
                source_refs=bar["source_refs"],
            )
            return order.price
        return self.h.quote(order.order_book_id)["last_price"]

    def _resting_bar(self, order):
        h = self.h
        result = h.owner.call(
            "market",
            security=order.order_book_id,
            label=h.at.isoformat(),
            as_of=h.at.isoformat(),
            frequency="1m",
            role="matching",
        )
        if result["status"] != "available" or len(result["rows"]) != 1:
            raise RuntimeError("SECOND_RESTING_MINUTE_MISSING")
        return result["rows"][0]

    def _get_bar_volume(self, order, open_auction=False):
        h = self.h
        # Existing finite research policy: same-day retrospective daily capacity.
        return h.daily(order.order_book_id, str(h.at.date()), "volume")

    def _get_liquidity_limited_fill(self, order, instrument, open_auction=False):
        h = self.h
        if (
            h.case.policy not in {"fixed", "integrated"}
            or order.datetime == h.at.replace(tzinfo=None)
            or (h.case.policy == "fixed" and not order.filled_quantity)
        ):
            return super()._get_liquidity_limited_fill(order, instrument, open_auction)
        from .policies import resting_capacity

        result = h.owner.call(
            "market",
            security=order.order_book_id,
            label=h.at.isoformat(),
            as_of=h.at.isoformat(),
            frequency="1m",
            role="matching",
        )
        if result["status"] != "available":
            raise RuntimeError("SECOND_RESTING_MINUTE_CAPACITY_MISSING")
        volume = result["rows"][0]["values"]["volume"]
        capacity = resting_capacity(
            volume, h.case.volume_ratio, int(self._turnover[order.order_book_id])
        )
        h.emit(
            "resting_capacity",
            volume=volume,
            ratio=h.case.volume_ratio,
            capacity=capacity,
            lot_rounding=False,
            strategy_visible=False,
            source_refs=result["rows"][0]["source_refs"],
        )
        if capacity <= 0:
            raise OrderNotMatchable("No whole-share capacity at this minute")
        return min(order.unfilled_quantity, capacity)
