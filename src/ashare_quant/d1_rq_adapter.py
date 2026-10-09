"""Read-only native RQAlpha adapter preparation for the frozen BR1 snapshot.

Instantiating DataProxy/BarObject exercises native read interfaces, not the event
loop, broker, account, or strategy. Registration/startup remains hard blocked.
"""

from datetime import date, datetime, time

import numpy as np
import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, TRADING_CALENDAR_TYPE
from rqalpha.data.data_proxy import DataProxy
from rqalpha.interface import AbstractDataSource, AbstractPriceBoard
from rqalpha.model.instrument import Instrument

from rqprobe.runtime import runtime_identity

from .contracts import SHANGHAI, DomainError
from .d1_br1 import profile, refuse_br1_execution
from .d1_codec import BoundPublicSnapshot
from .d1_native import AD08Call, rq_date

SID = "600000.XSHG"


def model_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise DomainError("BR1_NATIVE_CLOCK_INVALID", "Native model clock must be a datetime", 422)
    return value.astimezone(SHANGHAI).replace(tzinfo=None) if value.tzinfo else value


def model_day(value) -> date:
    if isinstance(value, datetime):
        return model_datetime(value).date()
    if type(value) is date:
        return value
    raise DomainError("BR1_NATIVE_CLOCK_INVALID", "Expected native date or model datetime", 422)


def stop(part: str):
    raise DomainError("BR1_NATIVE_READ_UNSUPPORTED", "Outside prepared native read scope", 422,
                      details={"part": part})


class BoundedInstrument(Instrument):
    """A-NORMAL eligibility is bounded; never manufacture a future delisting date."""

    def _bounded(self, dt):
        day = model_day(dt)
        if not date(2020, 1, 2) <= day <= date(2020, 1, 7):
            stop("instrument eligibility outside profile")

    def active_at(self, dt):
        self._bounded(dt)
        return True  # The declared A-NORMAL projection, not original historical_eligible.

    def de_listed_at(self, dt):
        self._bounded(dt)
        return False

    @property
    def de_listed_date(self):
        stop("absolute delisting date is unknown and unused")


class BR1DataSource(AbstractDataSource):
    execution_permission = False

    def __init__(self, snapshot: BoundPublicSnapshot):
        runtime_identity()
        snapshot.validate()
        self.snapshot = snapshot
        self._bars = {date.fromisoformat(r["trade_date"]): r for r in snapshot.public("br1.bars")}
        self._statuses = {date.fromisoformat(r["trade_date"]): r for r in snapshot.public("br1.statuses")}
        calendar = snapshot.public("br1.calendar")
        days = [r["trade_date"] for r in calendar if r["is_trading_day"]]
        # Explicit owner previous_open supports Jan-02's AD08; no synthetic padding.
        self._calendar = pd.DatetimeIndex([calendar[0]["previous_open"], *days])
        facts, rules = snapshot.public("br1.instrument")["fact"], snapshot.public("br1.rules")
        self._instrument = BoundedInstrument(dict(
            order_book_id=facts["security"], symbol=facts["name"], type="CS", exchange=facts["exchange"],
            listed_date=facts["initial_listing_date"], round_lot=rules["buy_round_lot"],
            board_type="MainBoard", market_tplus=1))
        self._at, self._phase = None, None
        self.actual_history_calls = []  # Read-call trace only; never an execution ledger.

    def phase(self, logical_at: datetime, phase: str) -> None:
        at = model_datetime(logical_at)
        if at.date() not in self._bars or phase not in {"BEFORE_TRADING", "BAR", "AFTER_TRADING"}:
            stop("phase or day")
        if ((phase == "BEFORE_TRADING" and at.time() >= time(15))
                or (phase == "BAR" and at.time() != time(15))
                or (phase == "AFTER_TRADING" and at.time() < time(15))):
            stop("phase clock")
        if self._at is not None and at < self._at:
            stop("clock regression")
        self._at, self._phase = at, phase

    def _day(self):
        if self._at is None:
            stop("native phase not supplied")
        return self._at.date()

    @staticmethod
    def _sid(sid):
        if sid != SID:
            stop("security")

    def get_instruments(self, id_or_syms=None, types=None):
        if id_or_syms is not None and any(s != SID for s in id_or_syms):
            stop("instrument query")
        if types is not None and any(t != INSTRUMENT_TYPE.CS for t in types):
            stop("instrument type")
        return [self._instrument]

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: self._calendar.copy()}

    def available_data_range(self, frequency):
        if frequency != "1d":
            stop("frequency")
        return min(self._bars), max(self._bars)

    def get_bar(self, instrument, dt, frequency):
        self._sid(instrument.order_book_id)
        day = model_day(dt)
        if frequency != "1d" or day != self._day() or self._phase == "BEFORE_TRADING":
            stop("daily bar not available in this phase")
        r, limits = self._bars[day], self.snapshot.public("br1.price_limits:" + day.isoformat())
        result = {k: float(r[k]) for k in ("open", "high", "low", "close")}
        result.update(datetime=int(day.strftime("%Y%m%d") + "000000"), volume=r["volume"],
                      limit_up=float(limits["upper"]), limit_down=float(limits["lower"]))
        # amount/total_turnover is intentionally absent, never manufactured as price*volume.
        return result

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True,
                     include_now=False, adjust_type="pre", adjust_orig=None):
        self._sid(instrument.order_book_id)
        if adjust_orig is None:
            stop("missing adjust_orig")
        call = AD08Call(security=instrument.order_book_id, trade_date=self._day(),
                        history_dt=rq_date(dt), adjust_orig=rq_date(adjust_orig), frequency=frequency,
                        field=fields, bar_count=bar_count, skip_suspended=skip_suspended,
                        include_now=include_now, adjustment_requested=adjust_type)
        if call not in profile().ad08_calls:
            stop("unreviewed AD08 tuple")
        self.actual_history_calls.append(call)
        value = self.snapshot.public("br1.prev_close:" + call.trade_date.isoformat())
        if value["call"] != call.model_dump(mode="json"):
            stop("AD08 identity")
        return np.array([float(value["value"])], dtype="float64")

    def _status(self, sid, dates, field):
        self._sid(sid)
        result = []
        for dt in dates:
            day = model_day(dt)
            if day not in self._statuses or day > self._day():
                stop("status outside current model day")
            value = self._statuses[day][field]
            if value is not False:
                stop("unknown or active suspended/ST flag")
            result.append(value)
        return result

    def is_suspended(self, order_book_id, dates):
        return self._status(order_book_id, dates, "suspended")

    def is_st_stock(self, order_book_id, dates):
        return self._status(order_book_id, dates, "is_st")

    def _empty_model_events(self, sid):
        self._sid(sid)
        self._day()
        projection = self.snapshot.public("br1.modeled_events")
        if projection["modeled_events"] or projection["evidence_status"] != "assumed_no_relevant_events":
            stop("related event")
        return None  # RQ empty dividend/split convention, only under explicit A-EQ.

    def get_dividend(self, instrument):
        return self._empty_model_events(instrument.order_book_id)

    def get_split(self, instrument):
        return self._empty_model_events(instrument.order_book_id)

    def get_share_transformation(self, order_book_id):
        return self._empty_model_events(order_book_id)

    def current_snapshot(self, *args, **kwargs):
        stop("intraday snapshot")

    def get_open_auction_bar(self, *args, **kwargs):
        stop("open auction")

    def history_ticks(self, *args, **kwargs):
        stop("ticks")

    def get_trading_minutes_for(self, *args, **kwargs):
        stop("minutes")


class BR1PriceBoard(AbstractPriceBoard):
    def __init__(self, ds: BR1DataSource):
        self._ds = ds

    def get_last_price(self, order_book_id):
        ds = self._ds
        ds._sid(order_book_id)
        day = ds._day().isoformat()
        if ds._phase == "BEFORE_TRADING":
            return float(ds.snapshot.public("br1.prev_close:" + day)["value"])
        return float(ds._bars[ds._day()]["close"])

    def _limit(self, sid, field):
        ds = self._ds
        ds._sid(sid)
        return float(ds.snapshot.public("br1.price_limits:" + ds._day().isoformat())[field])

    def get_limit_up(self, order_book_id):
        return self._limit(order_book_id, "upper")

    def get_limit_down(self, order_book_id):
        return self._limit(order_book_id, "lower")


def prepare_native_reads(snapshot: BoundPublicSnapshot):
    ds = BR1DataSource(snapshot)
    board = BR1PriceBoard(ds)
    return ds, board, DataProxy(ds, board)


def register_native_execution(env, *args, **kwargs):
    # No env access, event listener, broker, worker, or queue before this refusal.
    refuse_br1_execution()
