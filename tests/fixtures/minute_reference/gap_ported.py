"""One free JQ reference run, 2026-10-09..12, minute, CNY 10,000,000.

This is an observation experiment, not an investable strategy. No factor API,
paid data, credentials, or live/simulated trading setup is used. Orders deliberately
bind the participation cap. Read the complete log before drawing conclusions.
"""
from aquant import *
import json
import math

CASE = "minute-gaps-r4-v1"
STOCKS = ["600000.XSHG", "000001.XSHE"]
RATIO = 0.0001
CLOCKS = ["09:30", "09:31", "09:35", "09:40", "11:29", "11:30",
          "12:00", "13:00", "13:01", "13:02", "14:56", "14:57", "14:59", "15:00"]


def clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        return clean(value.item())
    return str(value)


def fields(obj, names):
    result = {}
    for key in names.split():
        try:
            result[key] = clean(getattr(obj, key))
        except Exception as exc:
            result[key] = {"unavailable": type(exc).__name__}
    return result


def emit(context, kind, **values):
    g.seq += 1
    log.info("AQGAP_JSON " + json.dumps(clean(dict(
        seq=g.seq, case=CASE, time=str(context.current_dt), kind=kind, **values)),
        sort_keys=True, allow_nan=False))


def order_fields(obj):
    return fields(obj, "order_id security amount filled is_buy status price avg_cost commission add_time")


def account(context, label):
    emit(context, "account", label=label,
         account=fields(context.portfolio, "starting_cash available_cash locked_cash positions_value total_value"),
         positions=[dict(security=s, **fields(p, "total_amount closeable_amount today_amount avg_cost price value"))
                    for s, p in sorted(context.portfolio.positions.items())],
         orders=[order_fields(o) for o in get_orders().values()],
         open_orders=[str(k) for k in get_open_orders()],
         trades=[fields(t, "trade_id order_id security amount price time") for t in get_trades().values()])


def market(context, label):
    rows = {}
    current = get_current_data()
    for sid in STOCKS:
        item = {"current": fields(current[sid], "last_price day_open paused high_limit low_limit")}
        try:
            frame = attribute_history(sid, 2, "1m", ("open", "high", "low", "close", "volume"),
                                      skip_paused=False, df=True, fq=None)
            item["history"] = [dict(label=str(i), **r.to_dict()) for i, r in frame.iterrows()]
        except Exception as exc:
            item["history_error"] = {"type": type(exc).__name__, "message": str(exc)}
        rows[sid] = item
    emit(context, "market", label=label, securities=rows)


def submit(context, label, sid, limit=None):
    emit(context, "submit", label=label, security=sid, amount=100000, limit=limit,
         available_cash=context.portfolio.available_cash,
         current=fields(get_current_data()[sid], "last_price day_open paused"))
    result = order(sid, 100000) if limit is None else order(sid, 100000, LimitOrderStyle(limit))
    emit(context, "return", label=label, order=None if result is None else order_fields(result))
    account(context, "after:" + label)


def initialize(context):
    g.seq = 0
    g.day = 0
    g.clocks = []
    g.order_state = None
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_option("order_volume_ratio", RATIO)
    set_order_cost(OrderCost(open_tax=0, close_tax=0.0005, open_commission=0.0003,
                            close_commission=0.0003, close_today_commission=0, min_commission=5), type="stock")
    set_slippage(FixedSlippage(0), type="stock")
    for clock in CLOCKS:
        run_daily(boundary, time=clock)
    emit(context, "identity", stocks=STOCKS, ratio=RATIO, cash=10000000,
         dates=["2026-10-09", "2026-10-12"],
         run_params=fields(context.run_params, "type frequency start_date end_date"))


def before_trading_start(context):
    g.day += 1
    g.clocks = []
    emit(context, "before_trading", day=g.day)
    account(context, "before_trading")


def boundary(context):
    clock = context.current_dt.strftime("%H:%M")
    emit(context, "scheduled", clock=clock, handle_count=len(g.clocks))
    market(context, "scheduled:" + clock)
    account(context, "scheduled:" + clock)
    if g.day != 1:
        return
    if clock == "09:40":
        submit(context, "market-A1", STOCKS[0])
        submit(context, "market-A2", STOCKS[0])
        submit(context, "market-B1", STOCKS[1])
    elif clock in ("11:29", "14:56"):
        price = round(get_current_data()[STOCKS[0]].last_price + 0.10, 2)
        submit(context, "limit-" + clock, STOCKS[0], price)
    elif clock == "13:02":
        for obj in list(get_open_orders().values()):
            emit(context, "cancel", order=order_fields(obj))
            cancel_order(obj)
        account(context, "after_cancel")


def handle_data(context, data):
    clock = context.current_dt.strftime("%H:%M")
    g.clocks.append(clock)
    if clock in CLOCKS:
        emit(context, "handle_data", clock=clock, handle_count=len(g.clocks))
    state = [order_fields(o) for o in get_orders().values()]
    encoded = json.dumps(clean(state), sort_keys=True)
    if encoded != g.order_state:
        g.order_state = encoded
        emit(context, "orders_changed", orders=state)
        market(context, "orders_changed")
        account(context, "orders_changed")


def after_trading_end(context):
    emit(context, "after_trading", day=g.day, handle_clocks=g.clocks)
    account(context, "after_trading")
    for sid in STOCKS:
        # Closing-time observation only: never consumed by an order decision.
        try:
            frame = get_price(sid, start_date=str(context.current_dt.date()),
                              end_date=str(context.current_dt.date()), frequency="daily",
                              fields=["close", "volume"], fq=None)
            emit(context, "closed_daily", security=sid,
                 rows=[dict(label=str(i), **r.to_dict()) for i, r in frame.iterrows()])
        except Exception as exc:
            emit(context, "closed_daily_error", security=sid,
                 error=type(exc).__name__, message=str(exc))


def on_strategy_end(context):
    account(context, "strategy_end")
    emit(context, "complete", days=g.day)
