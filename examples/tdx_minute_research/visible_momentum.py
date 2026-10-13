"""A different real Python strategy: choose using only visible five-minute returns."""

from jqdata import (
    LimitOrderStyle,
    g,
    get_current_data,
    history,
    order_target,
    order_value,
    record,
    run_daily,
)


def initialize(context):
    g.stocks = ["600000.XSHG", "000001.XSHE"]
    g.day = 0
    run_daily(select, "09:50")
    run_daily(close, "13:10")


def before_trading_start(context):
    g.day += 1


def select(context):
    if g.day != 1:
        return
    prices = history(5, "1m", "close", g.stocks, fq=None)
    assert prices.index[-1] <= context.current_dt
    # No source future prices, date-specific stock pick, or synthetic signal table.
    scores = {s: prices[s].iloc[-1] / prices[s].iloc[0] - 1 for s in g.stocks}
    g.selected = max(g.stocks, key=lambda s: scores[s])
    record(score_a=float(scores[g.stocks[0]]), score_b=float(scores[g.stocks[1]]))
    order_value(g.selected, context.portfolio.available_cash * 0.8)


def close(context):
    if g.day == 2:
        quote = get_current_data()[g.selected].last_price
        order_target(g.selected, 0, LimitOrderStyle(round(quote * 0.99, 2)))


def after_trading_end(context):
    if g.day == 2:
        assert not context.portfolio.positions
