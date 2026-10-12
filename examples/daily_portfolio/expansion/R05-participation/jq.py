from jqdata import *
import datetime
import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR, localcontext

# Frozen daily reference; UI dates, frequency and cash must match the manifest.
STRATEGY = 'daily-equal-weight'
WINDOW = ('2020-02-03', '2020-02-21')
SECURITIES = ['000001.XSHE', '000651.XSHE', '600000.XSHG', '600519.XSHG']


def clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {'nonfinite': str(value)}
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item'):
        return clean(value.item())
    return str(value)


def emit(context, kind, **value):
    event = dict(seq=len(g.events)+1, strategy=STRATEGY, time=str(context.current_dt), kind=kind, **value)
    line = json.dumps(clean(event), sort_keys=True, ensure_ascii=False, allow_nan=False)
    g.events.append(line)
    log.info('AQREAL_JSON ' + line)


def fields(obj, names):
    values = {}
    for name in names:
        try:
            values[name] = clean(getattr(obj, name))
        except Exception as exc:
            values[name] = {'unavailable': type(exc).__name__}
    return values


def snapshot(context, label):
    emit(context, 'snapshot', label=label,
         account=fields(context.portfolio, ['starting_cash', 'available_cash', 'locked_cash', 'positions_value', 'total_value', 'returns']),
         positions=[fields(p, ['security', 'total_amount', 'closeable_amount', 'today_amount', 'price', 'avg_cost', 'value']) for p in context.portfolio.positions.values()],
         orders=[fields(o, ['order_id', 'security', 'is_buy', 'amount', 'filled', 'status', 'price', 'commission', 'add_time']) for o in get_orders().values()],
         trades=[fields(t, ['trade_id', 'order_id', 'security', 'time', 'amount', 'price']) for t in get_trades().values()],
         open_orders=list(get_open_orders().keys()))


def initialize(context):
    g.events, g.days, g.targets = [], 0, None
    g.prior_equity = '1000000.00'
    g.file = 'AQREAL_' + STRATEGY + '_' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.jsonl'
    assert str(context.run_params.start_date)[:10] == WINDOW[0]
    assert str(context.run_params.end_date)[:10] == WINDOW[1]
    assert context.run_params.frequency in ('day', 'daily'), repr(context.run_params.frequency)
    assert context.portfolio.starting_cash == 1000000
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_option('order_volume_ratio', 0.0001)
    set_slippage(FixedSlippage(0))
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001, open_commission=0.0003,
                            close_commission=0.0003, close_today_commission=0, min_commission=5), type='stock')
    run_daily(submit, time='open')
    emit(context, 'identity', window=WINDOW, securities=SECURITIES, frequency=context.run_params.frequency, initial_cash=1000000,
         case_id='R05-participation', volume_ratio='0.0001',
         fee_model='commission 0.0003 min5; sell tax0.001; no slippage; platform-native dividend tax may differ from AQ gross model',
         data_basis='raw close; previous completed exchange sessions', file=g.file)


def before_trading_start(context):
    day, previous = context.current_dt.date(), context.previous_date
    rebalance = (g.days == 0 or STRATEGY in ('trend', 'daily-equal-weight') or
                 (STRATEGY == 'equal-weight' and (day.year, day.month) != (previous.year, previous.month)) or
                 (STRATEGY in ('momentum', 'weekly-reversal') and day.isocalendar()[:2] != previous.isocalendar()[:2]))
    g.days += 1
    g.targets = None
    if not rebalance:
        emit(context, 'decision', rebalance=False, previous_date=str(previous))
        return
    history = {}
    for sid in sorted(SECURITIES):
        frame = get_price(sid, end_date=previous, count=120, frequency='daily', fields=['close'], skip_paused=False, fq=None)
        assert len(frame) == 120, (sid, len(frame))
        assert max(frame.index).date() <= previous
        history[sid] = [str(v) for v in frame['close'].tolist()]
        assert all(Decimal(v).is_finite() and Decimal(v) > 0 for v in history[sid])
        emit(context, 'history', security=sid, dates=[str(v)[:10] for v in frame.index], raw_close=history[sid], fq=None)
    with localcontext() as ctx:
        ctx.prec = 50
        prices = {s: [Decimal(v) for v in history[s]] for s in sorted(SECURITIES)}
        scores = {}
        if STRATEGY in ('equal-weight', 'daily-equal-weight'):
            selected, n = sorted(SECURITIES), len(SECURITIES)
        elif STRATEGY == 'weekly-reversal':
            scores = {s: prices[s][-1] / prices[s][-6] - 1 for s in prices}
            selected, n = sorted(prices, key=lambda s: (scores[s], s))[:2], 2
        elif STRATEGY == 'momentum':
            scores = {s: prices[s][-1] / prices[s][-61] - 1 for s in prices}
            selected, n = sorted(prices, key=lambda s: (-scores[s], s))[:3], 3
        else:
            scores = {s: sum(prices[s]) / 120 for s in prices}
            selected, n = [s for s in prices if prices[s][-1] > scores[s]], 8
        weights = {s: Decimal('0.98') / n if s in selected else Decimal(0) for s in prices}
        g.targets = {s: int((Decimal(g.prior_equity) * weights[s] / prices[s][-1] / 100).to_integral_value(ROUND_FLOOR)) * 100 for s in prices}
    emit(context, 'decision', rebalance=True, previous_date=str(previous), prior_equity=g.prior_equity,
         selected=selected, scores=scores, weights=weights, targets=g.targets)


def submit(context):
    if g.targets is None:
        return
    snapshot(context, 'before_orders')
    deltas = {}
    for sid in sorted(SECURITIES):
        position = context.portfolio.positions.get(sid)
        deltas[sid] = g.targets[sid] - (0 if position is None else position.total_amount)
    plan = [(s, q) for s, q in deltas.items() if q < 0] + [(s, q) for s, q in deltas.items() if q > 0]
    for sid, qty in plan:
        state = get_current_data()[sid]
        emit(context, 'submit', security=sid, quantity=qty, cash_before=context.portfolio.available_cash,
             market=fields(state, ['day_open', 'last_price', 'paused', 'high_limit', 'low_limit', 'is_st']))
        result = order(sid, qty, style=MarketOrderStyle())
        emit(context, 'order_return', security=sid, returned_none=result is None,
             order=None if result is None else fields(result, ['order_id', 'security', 'is_buy', 'amount', 'filled', 'status', 'price', 'commission']),
             cash_after=context.portfolio.available_cash)
    snapshot(context, 'after_orders')


def after_trading_end(context):
    for sid in sorted(SECURITIES):
        frame = get_price(sid, start_date=context.current_dt.date(), end_date=context.current_dt.date(), frequency='daily', fields=['open','high','low','close','volume','money','paused','high_limit','low_limit'], skip_paused=False, fq=None)
        emit(context, 'daily_raw', security=sid, records=frame.reset_index().to_dict('records'))
    snapshot(context, 'day_close')
    g.prior_equity = str(context.portfolio.total_value)


def on_strategy_end(context):
    snapshot(context, 'strategy_end')
    content = '\n'.join(g.events) + '\n'
    write_file(g.file, content, append=False)
    log.info('AQREAL_EXPORT ' + json.dumps(dict(file=g.file, records=len(g.events), bytes=len(content.encode('utf-8')),
             sha256=hashlib.sha256(content.encode('utf-8')).hexdigest()), sort_keys=True))
