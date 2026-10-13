"""Real-source two-bank example: one cash account, T+1, next-day liquidation.

Uses the supported synchronous jqdata subset. Fees and the research data profile
are explicit run inputs. This is an execution example, not an investment signal.
"""

from jqdata import (
    LimitOrderStyle,
    g,
    get_current_data,
    get_open_orders,
    order,
    order_target,
    order_value,
    record,
    run_daily,
    set_universe,
)


def initialize(context):
    g.stocks = ["600000.XSHG", "000001.XSHE"]
    g.day = 0
    set_universe(g.stocks)
    run_daily(rebalance, "09:40")
    run_daily(check_t1, "09:45")
    run_daily(report, "after_close")


def before_trading_start(context):
    g.day += 1


def rebalance(context):
    if g.day == 1:
        # Both requests reserve cash in the same native account before either fills.
        budget = context.portfolio.available_cash * 0.45
        for stock in g.stocks:
            assert order_value(stock, budget) is not None
        assert context.portfolio.locked_cash > budget
        assert order(g.stocks[0], 2000) is None  # insufficient remaining shared cash
        record(shared_cash_rejection=1, locked_cash=context.portfolio.locked_cash)
    else:
        for stock in g.stocks:
            position = context.portfolio.positions[stock]
            assert position.closeable_amount == position.total_amount > 0
            quote = get_current_data()[stock].last_price
            # A day limit can fill over several actual source-volume bars.
            order_target(stock, 0, LimitOrderStyle(round(quote * 0.99, 2)))


def check_t1(context):
    if g.day == 1:
        for stock in g.stocks:
            position = context.portfolio.positions[stock]
            assert position.total_amount > 0 and position.closeable_amount == 0
            assert order(stock, -100) is None
        record(t1_rejections=2)


def report(context):
    assert not get_open_orders()
    assert abs(context.portfolio.locked_cash) < 1e-8  # native binary-float arithmetic
    if g.day == 2:
        assert not context.portfolio.positions
    record(day=g.day, cash=context.portfolio.available_cash, equity=context.portfolio.total_value)
