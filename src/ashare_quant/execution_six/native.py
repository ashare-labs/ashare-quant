"""RQAlpha extension seams only; native matcher/account own all state changes."""

import sys
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, TRADING_CALENDAR_TYPE
from rqalpha.core.events import EVENT, Event
from rqalpha.core.strategy_universe import StrategyUniverse
from rqalpha.data.data_proxy import DataProxy
from rqalpha.interface import AbstractDataSource, AbstractEventSource, AbstractPriceBoard
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderCancelled, OrderNotMatchable
from rqalpha.model.instrument import Instrument
from rqalpha.portfolio.account import Account
from rqalpha.portfolio.capital_gains_tax import CapitalGainsTaxMixin

from ashare_quant.contracts import SHANGHAI

from .contracts import owner_price, owner_volume


def day(value):
    return value.date() if isinstance(value, datetime) else value


class ScopedInstrument(Instrument):
    def __init__(self, host):
        self.host = host
        super().__init__({"order_book_id": host.case.security, "symbol": host.case.security,
                          "type": "CS", "exchange": "XSHE", "round_lot": 100,
                          "board_type": "MainBoard", "market_tplus": 1})

    def listed_at(self, dt):
        return self.host.read("lifecycle", target_date=str(day(dt)))["listed"]

    def active_at(self, dt):
        return self.host.read("lifecycle", target_date=str(day(dt)))["active"]

    def de_listed_at(self, dt):
        if dt is None and sys._getframe(1).f_code is StockPosition.settlement.__code__:
            self.host.emit("terminal_lifecycle_model", next_session=None, delisting_fact=None,
                           model="no_terminal_delisting_adjustment_in_single_session")
            return False
        return self.host.read("lifecycle", target_date=str(day(dt)))["delisted"]

    @property
    def listed_date(self):
        raise RuntimeError("SIX_ABSOLUTE_LISTING_DATE_UNAVAILABLE")

    @property
    def de_listed_date(self):
        if sys._getframe(1).f_code is not StrategyUniverse._clear_de_listed.__code__:
            raise RuntimeError("SIX_ABSOLUTE_DELISTING_DATE_UNAVAILABLE")
        # Internal native cleanup horizon only, never exposed as a historical date.
        successor = date.fromisoformat(self.host.metadata["calendar"][-1])
        if self.host.read("lifecycle", target_date=str(self.host.case.day))["delisted"]:
            raise RuntimeError("SIX_DELISTING_OUT_OF_SCOPE")
        return datetime.combine(successor + timedelta(days=1), time())


class Source(AbstractDataSource):
    def __init__(self, host):
        self.host = host
        self.instrument = ScopedInstrument(host)

    def get_instruments(self, id_or_syms=None, types=None):
        if id_or_syms is not None and any(x != self.host.case.security for x in id_or_syms):
            raise RuntimeError("SIX_SECURITY_OUT_OF_SCOPE")
        return [self.instrument] if types is None or INSTRUMENT_TYPE.CS in types else []

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: pd.DatetimeIndex(self.host.metadata["calendar"])}

    def available_data_range(self, frequency):
        if frequency != "1m":
            raise RuntimeError("SIX_ONLY_MINUTE_EXECUTION")
        return self.host.case.day, self.host.case.day

    def get_bar(self, instrument, dt, frequency):
        self.host.security(instrument.order_book_id)
        if frequency != "1m" or dt.replace(tzinfo=SHANGHAI) != self.host.at:
            raise RuntimeError("SIX_BAR_CLOCK_MISMATCH")
        value = self.host.read("bar")
        if value["at"] != self.host.at.isoformat():
            raise RuntimeError("SIX_BAR_LABEL_MISMATCH")
        result = {k: owner_price(value[k]) for k in ("open", "high", "low", "close")}
        quote = self.host.read("quote")
        result.update(datetime=int(dt.strftime("%Y%m%d%H%M%S")), volume=owner_volume(value["volume"]),
                      total_turnover=float(value["amount"]),
                      limit_up=owner_price(quote["upper_limit"]), limit_down=owner_price(quote["lower_limit"]))
        return result

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True,
                     include_now=False, adjust_type="pre", adjust_orig=None):
        self.host.security(instrument.order_book_id)
        if not (bar_count == 1 and frequency == "1d" and fields == "close"
                and day(dt).isoformat() == self.host.metadata["calendar"][0]):
            raise RuntimeError("SIX_HISTORY_QUERY_UNSUPPORTED")
        value = self.host.read("previous_close", requested_adjustment=adjust_type)
        return np.array([owner_price(value["close"])], dtype="float64")

    def is_suspended(self, order_book_id, dates):
        self.host.security(order_book_id)
        if any(day(d) != self.host.case.day for d in dates):
            raise RuntimeError("SIX_STATUS_DATE_OUT_OF_SCOPE")
        return [self.host.read("quote")["suspended"] for _ in dates]

    def is_st_stock(self, order_book_id, dates):
        self.host.security(order_book_id)
        if any(day(d) != self.host.case.day for d in dates):
            raise RuntimeError("SIX_STATUS_DATE_OUT_OF_SCOPE")
        return [self.host.read("quote")["is_st"] for _ in dates]

    def get_dividend(self, instrument):
        raise RuntimeError("SIX_CORPORATE_ACTIONS_UNSUPPORTED")

    def get_split(self, instrument):
        raise RuntimeError("SIX_CORPORATE_ACTIONS_UNSUPPORTED")

    def get_share_transformation(self, order_book_id):
        raise RuntimeError("SIX_SHARE_TRANSFORMATION_UNSUPPORTED")

    def current_snapshot(self, *args, **kwargs):
        raise RuntimeError("SIX_AGGREGATE_SNAPSHOT_UNSUPPORTED")


class Board(AbstractPriceBoard):
    def __init__(self, host):
        self.host = host

    def get_last_price(self, sid):
        self.host.security(sid)
        if self.host.at.time() >= time(15, 30):
            return owner_price(self.host.read("valuation")["close"])
        return owner_price(self.host.read("quote")["last"])

    def get_limit_up(self, sid):
        self.host.security(sid)
        return owner_price(self.host.read("quote")["upper_limit"])

    def get_limit_down(self, sid):
        self.host.security(sid)
        return owner_price(self.host.read("quote")["lower_limit"])


class Proxy(DataProxy):
    def zero_tax_period(self, caller, field):
        portfolio = caller.f_locals["self"]
        if (portfolio._annual_deductible_balance != 0 or portfolio._monthly_realized_pnl != 0
                or portfolio._env.config.base.capital_gain_tax_rate != 0):
            raise RuntimeError("SIX_TAX_PERIOD_REQUIRES_REAL_CALENDAR")
        value = getattr(self._data_source.host.case.day, field)
        self._data_source.host.emit("zero_tax_period_model", field=field, value=value,
                                    adjacent_session=None, realized_pnl="0", tax_rate="0")
        # Only a period scalar for inert zero-basis/zero-tax checks, not a date.
        return SimpleNamespace(**{field: value})

    def get_previous_trading_date(self, *args, **kwargs):
        if len(self._data_source.host.metadata["calendar"]) == 1:
            caller = sys._getframe(1)
            if caller.f_code is Account.__init__.__code__ and caller.f_locals.get("init_positions") == {}:
                # Fixed native code only uses this local inside the non-empty
                # initial-position loop. None is an unused marker, not a date.
                self._data_source.host.emit("unused_previous_session", value=None,
                                            native_consumer="empty_Account.__init__")
                return None
            if caller.f_code is CapitalGainsTaxMixin._is_start_of_year.__code__:
                return self.zero_tax_period(caller, "year")
            raise RuntimeError("SIX_PREVIOUS_SESSION_UNKNOWN")
        return super().get_previous_trading_date(*args, **kwargs)

    def get_next_trading_date(self, *args, **kwargs):
        if len(self._data_source.host.metadata["calendar"]) == 1:
            if sys._getframe(1).f_code is StockPosition.settlement.__code__:
                # Consumed only by the explicit terminal lifecycle model above;
                # no fabricated next date or next-day market read.
                return None
            if sys._getframe(1).f_code is CapitalGainsTaxMixin._is_end_of_month.__code__:
                return self.zero_tax_period(sys._getframe(1), "month")
            raise RuntimeError("SIX_NEXT_SESSION_UNKNOWN")
        return super().get_next_trading_date(*args, **kwargs)

    def get_instrument_history(self, id_or_sym, listed_at=None):
        values = self._data_source.get_instruments([id_or_sym])
        if listed_at is not None and not values[0].listed_at(listed_at):
            raise RuntimeError("SIX_NOT_LISTED")
        return values


class Matcher(DefaultBarMatcher):
    """Only gate phase / bind price and capacity inputs. No fill/account code."""

    def __init__(self, host, config):
        self.host = host
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        if self.host.at.time() < time(9, 30):
            raise OrderNotMatchable("AQ pre-open order retained until the declared opening pulse")
        if self.host.opening_pulse:
            return owner_price(self.host.read("opening_price")["price"])
        actual = self.host.read("bar")
        if owner_volume(actual["volume"]) == 0:
            raise OrderCancelled("AQ source minute has zero volume")
        return super()._get_deal_price(order, instrument, open_auction)

    def _get_bar_volume(self, order, open_auction=False):
        if self.host.case.case_id in {"B10", "B13"}:
            value = self.host.read("capacity")
            self.host.emit("capacity_read", basis="retrospective_whole_day_volume",
                           volume=owner_volume(value["volume"]), ratio=self.host.case.volume_ratio,
                           strategy_visible=False)
            return owner_volume(value["volume"])
        if self.host.opening_pulse:
            return owner_volume(self.host.read("opening_volume")["volume"])
        return super()._get_bar_volume(order, open_auction)


class Events(AbstractEventSource):
    def __init__(self, host):
        self.host = host
        self.cursor = 0

    def events(self, start_date, end_date, frequency):
        kinds = {"before": EVENT.BEFORE_TRADING, "bar": EVENT.BAR,
                 "after": EVENT.AFTER_TRADING}
        for record in self.host.plan:
            self.host.at = datetime.fromisoformat(record["at"])
            native_at = self.host.at.replace(tzinfo=None)
            self.host.env.update_time(native_at, native_at)
            self.host.emit("clock", clock_kind=record["kind"], index=record["index"])
            yield Event(kinds.get(record["kind"], EVENT.USER), calendar_dt=native_at,
                        trading_dt=native_at, aq_kind=record["kind"])
            self.cursor = record["index"]
            self.host.emit("clock_committed", index=self.cursor)
