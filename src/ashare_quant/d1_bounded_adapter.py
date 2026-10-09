"""Native-only finite listing adapter. Registration through general API is blocked."""

import sys
from contextlib import contextmanager
from datetime import datetime, time, timedelta

from rqalpha.const import MARKET, TRADING_CALENDAR_TYPE
from rqalpha.core.events import EVENT
from rqalpha.core.executor import Executor
from rqalpha.core.strategy_universe import StrategyUniverse
from rqalpha.data.data_proxy import DataProxy
from rqalpha.model.instrument import Instrument

from rqprobe.checkpoint import AtomicFile

from .d1_boundary import NativeBoundary
from .d1_br1 import profile
from .d1_event_binding import EventBindings
from .d1_native import AD08Call, rq_date
from .d1_rq_adapter import SID, BR1DataSource, BR1PriceBoard, model_day, stop


class ListingInstrument(Instrument):
    def __init__(self, fields, boundary):
        super().__init__(fields)
        self._boundary = boundary
        self._bound_fields = dict(self._dict)
        self._check_fields()

    def _check_fields(self):
        if self._dict != self._bound_fields or self._dict.get("order_book_id") != SID:
            stop("native instrument identity drift")

    def listed_at(self, dt):
        self._check_fields()
        return self._boundary.projection(model_day(dt)).model_listed

    def active_at(self, dt):
        self._check_fields()
        value = self._boundary.projection(model_day(dt))
        return value.model_listed and not value.model_delisted

    def de_listed_at(self, dt):
        self._check_fields()
        return self._boundary.projection(model_day(dt)).model_delisted

    @property
    def de_listed_date(self):
        if sys._getframe(1).f_code is not StrategyUniverse._clear_de_listed.__code__:
            stop("unreviewed absolute-date consumer")
        self._check_fields()
        b = self._boundary
        if sys._getframe(1).f_locals.get("self") is not b.env._universe:
            stop("horizon marker requires owned native universe")
        if b.phase != "AFTER_TRADING":
            stop("horizon marker outside native cleanup")
        b.projection(b._at.date())
        # Internal encoding only: not an owner field, estimate or market date.
        return datetime.combine(max(b.owner.contract.query_dates) + timedelta(days=1), time())

    def days_from_listed(self):
        stop("unreviewed lifecycle consumer")

    def __repr__(self):
        stop("native instrument export")


class ListingDataSource(BR1DataSource):
    def __init__(self, boundary):
        super().__init__(boundary.owner.snapshot)
        self.boundary = boundary
        self._instrument = ListingInstrument(dict(self._instrument._dict), boundary)
        boundary._source = self
        self.sync_phase()

    def sync_phase(self, *, restoring=False):
        if restoring:
            self._at = None  # Only after validated checkpoint domain and native clock install.
        phase = self.boundary.phase
        mapped = (
            "BEFORE_TRADING"
            if phase in {"BOOT", "WARMUP"}
            else ("AFTER_TRADING" if phase == "SETTLEMENT" else phase)
        )
        super().phase(self.boundary._at, mapped)

    def phase(self, *args, **kwargs):
        stop("source phase must come from guarded native host")

    def _day(self):
        self.boundary.context()
        if self._at != self.boundary._at:
            stop("source and runtime clocks differ")
        return super()._day()

    def get_instruments(self, id_or_syms=None, types=None):
        self.boundary.projection(self.boundary._at.date())
        return super().get_instruments(id_or_syms, types)

    def get_trading_calendars(self):
        self.boundary.check_identity()
        return super().get_trading_calendars()

    def available_data_range(self, frequency):
        self.boundary.check_identity()
        return super().available_data_range(frequency)


class ListingDataProxy(DataProxy):
    """All guards are outside inherited RQ caches, including cache hits."""

    def __init__(self, source, board, market=MARKET.CN):
        if type(source) is not ListingDataSource or market != MARKET.CN:
            stop("bounded proxy construction")
        self.boundary = source.boundary
        self.boundary.life.begin_proxy(self, source, board)
        try:
            super().__init__(source, board, market=market)
            self.boundary.life.finish_proxy()
        except Exception:
            self.boundary.life.close("native proxy construction failed")
            raise

    def _current(self, sid=SID):
        if sid != SID:
            stop("security")
        return self.boundary.projection(self.boundary._at.date())

    def _calendar_access(self):
        b = self.boundary
        b.check_identity()
        # RQ enumerates the fixed config calendar before initializing its clock.
        # This exception grants no Instrument, bar, quote or state-query access.
        if b.phase == "BOOT" and b.life.stage in {"BUILDING", "READY"}:
            return
        b.context()

    def get_trading_calendar(self, trading_calendar_type=None):
        self._calendar_access()
        if trading_calendar_type not in (None, TRADING_CALENDAR_TYPE.CN_STOCK):
            stop("calendar type")
        return super().get_trading_calendar(trading_calendar_type).copy()

    def get_trading_dates(self, start_date, end_date, trading_calendar_type=None):
        b = self.boundary
        self._calendar_access()
        if (model_day(start_date), model_day(end_date)) != (
            b.owner.contract.event_dates[0],
            b.owner.contract.event_dates[-1],
        ):
            stop("calendar enumeration scope")
        if trading_calendar_type not in (None, TRADING_CALENDAR_TYPE.CN_STOCK):
            stop("calendar type")
        return super().get_trading_dates(start_date, end_date, trading_calendar_type)

    def get_active_instrument(self, id_or_sym, dt):
        self._current(id_or_sym)
        self.boundary.projection(model_day(dt))
        result = super().get_active_instrument(id_or_sym, dt)
        self._current(id_or_sym)
        return result

    def get_instrument_history(self, id_or_sym, listed_at=None):
        self._current(id_or_sym)
        if listed_at is not None:
            self.boundary.projection(model_day(listed_at))
        result = super().get_instrument_history(id_or_sym, listed_at)
        self._current(id_or_sym)
        return result

    def get_all_instruments(self, *args, **kwargs):
        stop("general instrument export")

    def get_instruments_history(self, *args, **kwargs):
        stop("general instrument history export")

    def get_bar(self, order_book_id, dt, frequency="1d"):
        self._current(order_book_id)
        b = self.boundary
        if (
            frequency != "1d"
            or model_day(dt) != b._at.date()
            or b.phase not in {"BAR", "AFTER_TRADING", "SETTLEMENT"}
        ):
            stop("cached bar outside current phase")
        value = super().get_bar(order_book_id, dt, frequency)
        self._current(order_book_id)
        return value

    def get_next_trading_date(self, date, n=1, trading_calendar_type=None):
        b = self.boundary
        self._current()
        if (
            type(n) is not int
            or n != 1
            or model_day(date) != b._at.date()
            or b.phase != "SETTLEMENT"
            or trading_calendar_type not in (None, TRADING_CALENDAR_TYPE.CN_STOCK)
        ):
            stop("successor request context")
        candidate = super().get_next_trading_date(date, n, trading_calendar_type)
        b.projection(model_day(candidate), successor=True, n=n)
        return candidate

    def get_previous_trading_date(self, date, n=1, trading_calendar_type=None):
        b = self.boundary
        self._current()
        day = model_day(date)
        executor_read = sys._getframe(1).f_code is Executor._ensure_before_trading.__code__
        if (
            type(n) is not int
            or n != 1
            or trading_calendar_type not in (None, TRADING_CALENDAR_TYPE.CN_STOCK)
            or (
                day != b._at.date()
                and not (
                    executor_read
                    and b.phase == "AFTER_TRADING"
                    and day in b.owner.contract.event_dates
                    and day > b._at.date()
                )
            )
        ):
            stop("previous-date request context")
        rows = {
            model_day(datetime.fromisoformat(r["trade_date"])): r
            for r in b.owner.snapshot.public("br1.calendar")
        }
        if day not in rows or rows[day]["previous_open"] is None:
            stop("previous-date source scope")
        candidate = super().get_previous_trading_date(date, n, trading_calendar_type)
        if candidate.date().isoformat() != rows[day]["previous_open"] or candidate.date() >= day:
            stop("previous-date source conflict")
        self._current()
        return candidate

    def get_prev_close(self, order_book_id, dt, adjust_type="pre"):
        self._current(order_book_id)
        b = self.boundary
        if model_day(dt) != b._at.date() or adjust_type not in {"pre", "none"}:
            stop("cached AD08 request")
        previous = self.get_previous_trading_date(dt)
        call = AD08Call(
            security=order_book_id,
            trade_date=b._at.date(),
            history_dt=rq_date(previous),
            adjust_orig=rq_date(dt.replace(hour=0, minute=0, second=0)),
            frequency="1d",
            field="close",
            bar_count=1,
            skip_suspended=False,
            include_now=False,
            adjustment_requested="pre",
        )
        if call not in profile().ad08_calls:
            stop("cached AD08 tuple")
        value = super().get_prev_close(order_book_id, dt, adjust_type)
        self._current(order_book_id)
        return value


class ListingCheckpoint(AtomicFile):
    def __init__(self, path, identity, boundary):
        if identity != boundary.identity:
            stop("checkpoint adapter identity")
        super().__init__(path, identity)
        self.boundary = boundary

    def read(self):
        self.boundary.check_identity()
        payload = super().read()  # Identity/hash before pickle, trusted local files only.
        if payload is not None:
            self.boundary.preflight_checkpoint(payload)
        return payload

    def completed_receipt(self):
        self.boundary.context()
        payload = super().read()
        if not payload or not payload["complete"]:
            stop("completed receipt requested before completion")
        self.boundary.validate_checkpoint(payload)
        return payload["receipt"]


class BoundedHost:
    """Read boundary wiring; the fixed isolated entry owns execution."""

    def __init__(self, env, owner, *, run_identity):
        self.boundary = NativeBoundary(env, owner, run_identity=run_identity)
        self.source = ListingDataSource(self.boundary)
        self.board = BR1PriceBoard(self.source)

    def bind_data(self, *, defer_events=False):
        self.boundary.life.bind_data(self.source, self.board)
        if not defer_events:
            self.bind_phase_events()

    def bind_phase_events(self):
        self.boundary.life.check()
        if self.boundary.life.events is not None or self.boundary.phase != "BOOT":
            self.boundary.life.reject("phase event binding is single use before the first event")
        callbacks = {}
        for event, phase in (
            (EVENT.PRE_BEFORE_TRADING, "BEFORE_TRADING"),
            (EVENT.PRE_BAR, "BAR"),
            (EVENT.PRE_AFTER_TRADING, "AFTER_TRADING"),
            (EVENT.PRE_SETTLEMENT, "SETTLEMENT"),
        ):
            callbacks[event] = lambda e, phase=phase: self.boundary.transition(phase)
        EventBindings(self.boundary.life, callbacks)
        self.boundary.life.check()

    def get_state(self):
        return self.boundary.get_state()

    def set_state(self, state):
        self.boundary.set_state(state)

    def close(self):
        self.boundary.life.close()

    @contextmanager
    def native_proxy_factory(self):
        import rqalpha.main

        original = rqalpha.main.DataProxy
        self.boundary.check_identity()

        def factory(source, board, market=MARKET.CN):
            if source is not self.source or board is not self.board:
                stop("native factory binding")
            return ListingDataProxy(source, board, market)

        rqalpha.main.DataProxy = factory
        try:
            yield
        finally:
            rqalpha.main.DataProxy = original
