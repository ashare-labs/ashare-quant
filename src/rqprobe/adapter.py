"""Schema/clock conversion only. Provider owns all data and admission decisions."""
from rqalpha.interface import AbstractDataSource, AbstractPriceBoard
from rqalpha.const import TRADING_CALENDAR_TYPE
from rqalpha.model.instrument import Instrument
from rqalpha.model.tick import TickObject
from .fixture import CapabilityError, aware

REQUIRED = frozenset({'calendar', 'instruments', 'raw_bars', 'adjustments', 'actions', 'status', 'limits', 'frozen_as_of'})


class DataSource(AbstractDataSource):
    def __init__(self, env, provider):
        missing = REQUIRED - provider.capabilities
        if missing:
            raise CapabilityError('MISSING_CAPABILITIES:' + ','.join(sorted(missing)))
        self.env, self.provider = env, provider
        self._instruments = [Instrument(d) for d in provider.instruments()]
        self._frame = None
        self._as_of = None

    def frame(self):
        as_of = aware(self.env.calendar_dt)
        if self._frame is None or as_of != self._as_of:
            self._frame, self._as_of = self.provider.at(as_of), as_of
        return self._frame

    def get_instruments(self, id_or_syms=None, types=None):
        return [i for i in self._instruments if (id_or_syms is None or i.order_book_id in id_or_syms or i.symbol in id_or_syms)
                and (types is None or i.type in types)]

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: self.provider.calendar}

    def available_data_range(self, frequency):
        return self.provider.calendar[1].date(), self.provider.calendar[-2].date()

    def get_bar(self, instrument, dt, frequency):
        rows = self.frame().bars(instrument.order_book_id, frequency, dt, count=1)
        if not len(rows):
            return None
        expected = int(dt.strftime('%Y%m%d') + ('000000' if frequency == '1d' else dt.strftime('%H%M%S')))
        return rows[-1] if rows[-1]['datetime'] == expected else None

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True,
                     include_now=False, adjust_type='pre', adjust_orig=None):
        # dt is RQ's query boundary. Never use it as the strategy clock.
        rows = self.frame().bars(instrument.order_book_id, frequency, dt, bar_count, adjust_type, adjust_orig)
        return rows if fields is None else rows[fields]

    def is_suspended(self, order_book_id, dates):
        return self.frame().status(order_book_id, dates)

    def is_st_stock(self, order_book_id, dates):
        return self.frame().status(order_book_id, dates)

    def get_dividend(self, instrument):
        return self.frame().corporate_actions('dividend')

    def get_split(self, instrument):
        return self.frame().corporate_actions('split')

    def get_share_transformation(self, order_book_id):
        raise CapabilityError('SHARE_TRANSFORMATION_UNAVAILABLE')

    def current_snapshot(self, instrument, frequency, dt):
        raise CapabilityError('INTRADAY_AGGREGATE_SNAPSHOT_UNAVAILABLE')


class PriceBoard(AbstractPriceBoard):
    def __init__(self, ds):
        self.ds = ds

    def field(self, sid, field):
        rows = self.ds.frame().bars(sid, self.ds.env.config.base.frequency, self.ds.env.calendar_dt, 1)
        if not len(rows):
            raise CapabilityError('NO_VISIBLE_PRICE')
        return float(rows[-1][field])

    def get_last_price(self, sid): return self.field(sid, 'close')
    def get_limit_up(self, sid): return self.field(sid, 'limit_up')
    def get_limit_down(self, sid): return self.field(sid, 'limit_down')
