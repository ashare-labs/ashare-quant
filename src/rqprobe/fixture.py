"""SYNTHETIC data owner/test double, NOT an A数达 implementation or real market data.

All fabricated calendars, instrument facts, prices, adjustment and action records
live here, never in the engine adapter. No claim of real coverage/PIT/finality.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
SID = '600000.XSHG'
DTYPE = [('datetime', '<i8')] + [(f, '<f8') for f in
    ['open', 'high', 'low', 'close', 'volume', 'total_turnover', 'limit_up', 'limit_down']]


class CapabilityError(RuntimeError):
    pass


def aware(dt):
    return pd.Timestamp(dt).to_pydatetime().replace(tzinfo=TZ) if pd.Timestamp(dt).tzinfo is None else pd.Timestamp(dt).to_pydatetime().astimezone(TZ)


class SyntheticProvider:
    capabilities = frozenset({'calendar', 'instruments', 'raw_bars', 'adjustments',
        'actions', 'status', 'limits', 'frozen_as_of', 'recorded_events'})
    evidence_kind = 'synthetic'
    calendar = pd.DatetimeIndex(['2023-12-29', '2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08'])

    def __init__(self, actions=False, missing=(), tplus=1):
        self.actions = actions
        self.capabilities = self.capabilities - set(missing)
        self.tplus = tplus
        self.queries = []
        self.rows = {'1d': [], '1m': []}
        for day in self.calendar:
            price = 4.5 if actions and day.date() >= datetime(2024, 1, 3).date() else 10.0
            # Distinct open/close proves daily next_bar is changed to current_bar.
            self.rows['1d'].append(self.row(day, price, 240000, open_price=price - .5))
            for start in [day.replace(hour=9, minute=31), day.replace(hour=13, minute=1)]:
                for i in range(120):
                    self.rows['1m'].append(self.row(start + timedelta(minutes=i), price, 400))
        self.rows = {k: np.array(v, dtype=DTYPE) for k, v in self.rows.items()}

    @staticmethod
    def row(dt, p, v, open_price=None):
        return (int(dt.strftime('%Y%m%d%H%M%S')), p if open_price is None else open_price,
                p + .5, p - .5, p, v, p * v, p * 1.1, p * .9)

    def instruments(self):
        return [dict(order_book_id=SID, symbol='SYNTHETIC', type='CS', exchange='XSHG',
                     listed_date='2000-01-01', de_listed_date='2999-12-31', round_lot=100,
                     board_type='MainBoard', market_tplus=self.tplus, status='Active')]

    def at(self, as_of):
        return FrozenFixture(self, aware(as_of))

    def records(self, frequency, start='2024-01-02', end='2024-01-04'):
        result = []
        for day in self.calendar:
            if not pd.Timestamp(start) <= day <= pd.Timestamp(end):
                continue
            times = ['15:00'] if frequency == '1d' else ['09:31', '09:32', '09:33', '11:30', '13:01', '14:55', '15:00']
            for kind, tm in [('BEFORE_TRADING', '08:30')] + [('BAR', t) for t in times] + [('AFTER_TRADING', '15:30')]:
                result.append(dict(cursor=len(result)+1, event=kind, dt=f'{day.date()}T{tm}:00', evidence_kind='synthetic'))
        return result


class FrozenFixture:
    def __init__(self, parent, as_of):
        self.parent, self.as_of = parent, as_of
        self.snapshot_id = f'synthetic:{as_of.isoformat()}:{parent.actions}'

    def bars(self, sid, frequency, end, count=None, adjust_type='none', adjust_orig=None):
        if sid != SID:
            raise CapabilityError('UNKNOWN_INSTRUMENT')
        if frequency not in self.parent.rows:
            raise CapabilityError('UNSUPPORTED_FREQUENCY')
        if adjust_type != 'none' and 'adjustments' not in self.parent.capabilities:
            raise CapabilityError('ADJUSTMENTS_UNAVAILABLE')
        end = pd.Timestamp(end).tz_localize(None) if pd.Timestamp(end).tzinfo else pd.Timestamp(end)
        # RQ daily query dt is a date label; actual visibility uses independent as_of.
        end_key = int(end.strftime('%Y%m%d') + ('235959' if frequency == '1d' else end.strftime('%H%M%S')))
        rows = self.parent.rows[frequency]
        visible = []
        for row in rows:
            label = datetime.strptime(str(row['datetime']), '%Y%m%d%H%M%S').replace(tzinfo=TZ)
            bar_end = label.replace(hour=15) if frequency == '1d' else label
            if row['datetime'] <= end_key and bar_end <= self.as_of:
                visible.append(row)
        out = np.array(visible[-count:] if count else visible, dtype=DTYPE)
        # This hand-labelled synthetic split/dividend adjustment belongs to test data.
        if self.parent.actions and adjust_type == 'pre' and aware(adjust_orig or self.as_of).date() >= datetime(2024,1,3).date():
            for i in range(len(out)):
                if out[i]['datetime'] < 20240103000000:
                    for field in ['open', 'high', 'low', 'close']:
                        out[i][field] = (out[i][field] - 1) / 2
        self.parent.queries.append(dict(as_of=self.as_of.isoformat(), end=str(end), frequency=frequency,
                                        snapshot_id=self.snapshot_id, rows=len(out)))
        return out

    def status(self, sid, dates):
        if sid != SID:
            raise CapabilityError('UNKNOWN_INSTRUMENT')
        if 'status' not in self.parent.capabilities:
            raise CapabilityError('STATUS_UNAVAILABLE')
        return [False] * len(dates)

    def corporate_actions(self, kind):
        if 'actions' not in self.parent.capabilities:
            raise CapabilityError('ACTIONS_UNAVAILABLE')
        # Both events were declared before the experiment, so native cached_property
        # does not conceal late corporate-action arrivals in this fixture.
        if not self.parent.actions:
            return None
        if kind == 'dividend':
            return np.array([(20240102,20231229,1.,20240103,20240104,1.)], dtype=[
                ('book_closure_date','<i8'),('announcement_date','<i8'),('dividend_cash_before_tax','<f8'),
                ('ex_dividend_date','<i8'),('payable_date','<i8'),('round_lot','<f8')])
        return np.array([(20240103000000,2.)],dtype=[('ex_date','<i8'),('split_factor','<f8')])
