"""Public-owner native projection. No collection, raw-file reads or accounting."""
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, TRADING_CALENDAR_TYPE
from rqalpha.core.strategy_universe import StrategyUniverse
from rqalpha.data.data_proxy import DataProxy
from rqalpha.interface import AbstractDataSource, AbstractPriceBoard
from rqalpha.model.instrument import Instrument

from ashare_quant.contracts import SHANGHAI
from ashare_quant.m2.common import digest, require
from ashare_quant.m2.facade import read_previous_close
from ashare_quant.m2.native_bridge import PreparedRQBridge
from ashare_quant.m2.owner_wire import pack

SID = "600000.XSHG"


def day(value):
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    require(type(value) is date, "M2_NATIVE_DATE", "Expected native date or datetime")
    return value


class ScopedInstrument(Instrument):
    def __init__(self, host):
        self.host = host
        # Absolute listing/delisting dates are absent and never used as market facts.
        super().__init__(dict(order_book_id=SID, symbol=SID, type="CS", exchange="XSHG",
            round_lot=100, board_type="MainBoard", market_tplus=1))

    def listed_at(self, dt):
        return self.host.listing(day(dt))["model_listed"]

    def active_at(self, dt):
        value = self.host.listing(day(dt))
        return value["model_listed"] and not value["model_delisted"]

    def de_listed_at(self, dt):
        return self.host.listing(day(dt))["model_delisted"]

    @property
    def de_listed_date(self):
        require(sys._getframe(1).f_code is StrategyUniverse._clear_de_listed.__code__
                and self.host.phase == "AFTER_TRADING", "M2_UNKNOWN_ABSOLUTE_DATE", "Only native universe horizon cleanup may use this internal marker")
        self.host.listing(self.host.current_day)
        return datetime.combine(self.host.days[-1].next_open + timedelta(days=1), time())

    @property
    def listed_date(self):
        raise RuntimeError("M2_UNKNOWN_ABSOLUTE_LISTING_DATE")

    def days_from_listed(self):
        raise RuntimeError("M2_UNKNOWN_ABSOLUTE_LISTING_DATE")


class OwnerSource(AbstractDataSource):
    def __init__(self, host):
        self.host = host
        self.instrument = ScopedInstrument(host)
        self.calendar = pd.DatetimeIndex([host.days[0].previous_open, *[d.trading_date for d in host.days], host.days[-1].next_open])

    def get_instruments(self, id_or_syms=None, types=None):
        self.host.alive()
        require(id_or_syms is None or all(x == SID for x in id_or_syms), "M2_NATIVE_SECURITY", "Only selected security is supported")
        require(types is None or all(x == INSTRUMENT_TYPE.CS for x in types), "M2_NATIVE_TYPE", "Only ordinary stock is supported")
        self.host.listing(self.host.current_day)
        return [self.instrument]

    def get_trading_calendars(self):
        self.host.alive()
        return {TRADING_CALENDAR_TYPE.CN_STOCK: self.calendar.copy()}

    def available_data_range(self, frequency):
        self.host.alive()
        require(frequency == "1d", "M2_NATIVE_FREQUENCY", "Daily only")
        return self.host.days[0].trading_date, self.host.days[-1].trading_date

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True,
                     include_now=False, adjust_type="pre", adjust_orig=None):
        self.host.alive()
        require(self.host.phase == "DECIDE" and self.host.bridge is not None,
                "M2_NATIVE_AD08_PHASE", "Only the current decision's actual native AD08 may query P")
        bridge = self.host.bridge
        result = bridge.history_bars(instrument, bar_count, frequency, fields, dt, skip_suspended,
                                      include_now, adjust_type, adjust_orig)
        self.host.record("native_history_bars", {"call": bridge.read.query.to_dict()["call"],
            "argument_types": {"dt": type(dt).__name__, "adjust_orig": type(adjust_orig).__name__},
            "native_caller": sys._getframe(1).f_code.co_name,
            "shape": list(result.shape), "dtype": str(result.dtype), "return_repr": repr(result),
            "documents": pack(bridge.read).model_dump(mode="json")})
        return result

    def get_bar(self, instrument, dt, frequency):
        require(instrument.order_book_id == SID and frequency == "1d" and day(dt) == self.host.current_day,
                "M2_NATIVE_BAR_SCOPE", "Only current daily bar supported")
        operation = "execution_bar" if self.host.phase == "SUBMIT_MATCH" else "valuation_bar"
        r = self.host.engine(operation)
        # Limits are queried only at SUBMIT_MATCH, never reconstructed from P.
        limits = self.host.engine("trading_state") if self.host.phase == "SUBMIT_MATCH" else self.host.last_state()
        result = {k: float(r[k]) for k in ("open", "high", "low", "close")}
        result.update(datetime=int(self.host.current_day.strftime("%Y%m%d") + "000000"), volume=r["volume"],
            limit_up=float(limits["upper_limit"]), limit_down=float(limits["lower_limit"]))
        return result  # amount is not consumed or synthesized.

    def _status(self, sid, dates, field):
        require(sid == SID and all(day(d) == self.host.current_day for d in dates), "M2_NATIVE_STATUS_SCOPE", "Only current state supported")
        value = self.host.engine("trading_state")[field]
        return [value for _ in dates]

    def is_suspended(self, order_book_id, dates):
        return self._status(order_book_id, dates, "suspended")

    def is_st_stock(self, order_book_id, dates):
        return self._status(order_book_id, dates, "is_st")

    def _actions(self, sid):
        require(sid == SID, "M2_NATIVE_SECURITY", "Selected security required")
        r = self.host.engine("modeled_actions")
        require(r["modeled_events"] == [] and r["status"] == "assumed_no_relevant_events" and r["verified_absent"] is False,
                "M2_NATIVE_ACTION_SCOPE", "Only explicitly assumed empty model is prepared")
        return None

    def get_dividend(self, instrument):
        return self._actions(instrument.order_book_id)

    def get_split(self, instrument):
        return self._actions(instrument.order_book_id)

    def get_share_transformation(self, order_book_id):
        return self._actions(order_book_id)

    def current_snapshot(self, *a, **k):
        raise RuntimeError("M2_INTRADAY_NOT_ADMITTED")


class OwnerBoard(AbstractPriceBoard):
    def __init__(self, host):
        self.host = host

    def get_last_price(self, sid):
        require(sid == SID, "M2_NATIVE_SECURITY", "Selected security required")
        if self.host.phase == "DECIDE":
            # Engine valuation may consume the already observed P scalar, never T.
            require(self.host.previous is not None, "M2_NATIVE_P_NOT_OBSERVED", "Decision AD08 must precede engine P valuation")
            return float(self.host.previous.native_value)
        operation = "execution_bar" if self.host.phase == "SUBMIT_MATCH" else "valuation_bar"
        return float(self.host.engine(operation)["close"])

    def get_limit_up(self, sid):
        require(sid == SID, "M2_NATIVE_SECURITY", "Selected security required")
        return float(self.host.engine("trading_state")["upper_limit"])

    def get_limit_down(self, sid):
        require(sid == SID, "M2_NATIVE_SECURITY", "Selected security required")
        return float(self.host.engine("trading_state")["lower_limit"])


class OwnerProxy(DataProxy):
    def get_active_instrument(self, id_or_sym, dt):
        h = self._data_source.host
        h.alive()
        require(id_or_sym == SID, "M2_NATIVE_SECURITY", "Selected security required")
        h.listing(day(dt) if dt is not None else h.current_day)
        return super().get_active_instrument(id_or_sym, dt)

    def get_trading_calendar(self, trading_calendar_type=None):
        self._data_source.host.alive()
        require(trading_calendar_type in (None, TRADING_CALENDAR_TYPE.CN_STOCK), "M2_NATIVE_CALENDAR", "Only stock calendar")
        return super().get_trading_calendar(trading_calendar_type).copy()

    def get_instrument_history(self, id_or_sym, listed_at=None):
        # Single scoped identity has no ambiguous lifecycle history to sort.
        values = self._data_source.get_instruments(id_or_syms=[id_or_sym])
        if listed_at is not None:
            require(values[0].listed_at(listed_at), "M2_NATIVE_LISTING", "Outside modeled listing")
        return values

    def get_prev_close(self, sid, dt, adjust_type="pre"):
        h = self._data_source.host
        h.alive()
        require(sid == SID and adjust_type in {"pre", "none"} and day(dt) == h.current_day and h.phase in {"DECIDE", "SUBMIT_MATCH", "AFTER_TRADING", "SETTLEMENT"},
                "M2_NATIVE_PREVIOUS_SCOPE", "Previous native lookup is scoped to current day")
        # Keep the original RQ method and its cache; no cache clearing or substitute scalar.
        result = super().get_prev_close(sid, dt, adjust_type)
        h.record("native_proxy_return", {"requested_adjustment": adjust_type,
            "type": type(result).__name__, "repr": repr(result), "value": str(result)})
        return result

    def get_previous_trading_date(self, date, n=1, trading_calendar_type=None):
        h = self._data_source.host
        h.alive()
        target = day(date)
        expected = next((d.previous_open for d in h.days if d.trading_date == target), None)
        require(n == 1 and expected is not None, "M2_NATIVE_PREVIOUS_SCOPE", "Only frozen T/P pairs")
        value = super().get_previous_trading_date(date, n, trading_calendar_type)
        require(value.date() == expected, "M2_NATIVE_CALENDAR_DRIFT", "Native previous differs from owner plan")
        return value

    def get_next_trading_date(self, date, n=1, trading_calendar_type=None):
        h = self._data_source.host
        require(day(date) == h.current_day and n == 1, "M2_NATIVE_SUCCESSOR_SCOPE", "Only current settlement successor")
        expected = h.scope.next_open
        value = super().get_next_trading_date(date, n, trading_calendar_type)
        require(value.date() == expected, "M2_NATIVE_CALENDAR_DRIFT", "Native successor differs")
        h.engine("successor", candidate_date=expected, n=1)
        return value


class ActualPort:
    def __init__(self, host):
        self.host = host

    def current_context(self):
        return self.host.context()

    def read(self, context, call):
        h = self.host
        require(h.phase == "DECIDE" and context == h.context(), "M2_NATIVE_READ_PHASE", "Actual native port requires current decision")
        bridge = PreparedRQBridge(h.client, context)
        h.bridge = bridge
        value = h.env.data_proxy.get_prev_close(SID, context.logical_at.replace(tzinfo=None))
        origin = None
        if not bridge.history_attempts:
            require(h.previous is not None and h.previous.call == call, "M2_NATIVE_CACHE_ORIGIN", "Unobserved native cache entry")
            origin = h.previous.origin_fact
            bridge.authorize_cache(origin)
        observed = bridge.capture_return(value, origin=origin)
        call_id = origin.native_call_id if origin else f"native-ad08-{context.event_cursor}-{context.generation}"
        h.record("native_scalar_consumption", {"call_id": call_id, "cache_hit": origin is not None,
            "actual_history_calls": bridge.history_attempts, "native_type": type(value).__name__, "native_repr": repr(value),
            "owner_evidence": bridge.read.evidence.sha256, "authorization": bridge.read.authorization.sha256,
            "codec_observation": observed.codec_observation})
        return replace(observed, native_call_id=call_id)


def actual_previous(host):
    c, s = host.context(), host.client.session
    value = read_previous_close(ActualPort(host), s.entry, c, run_id=s.run_id, request=s.request)
    host.previous = value
    host.record("facade_consumption", {"evidence_sha256": digest(value), "native_call_id": value.native_call_id,
                                      "value": value.native_value, "cache_hit": value.cache_hit})
    return value
