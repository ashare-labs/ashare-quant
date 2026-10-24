"""Five-minute momentum rotation; ordinary Python control flow and pandas."""
# ruff: noqa: F405 -- APIs are supplied by the explicit aquant star import.
from aquant import *  # noqa: F403


def initialize(context):
    g.stocks = ["600000.XSHG", "000001.XSHE", "000002.XSHE"]
    run_daily(rebalance, time="09:35")


def rebalance(context):
    prices = history(5, "1m", "close", g.stocks, fq=None)
    returns = prices.iloc[-1] / prices.iloc[0] - 1
    winner = returns.idxmax()
    for stock, position in context.portfolio.positions.items():
        if stock != winner and position.closeable_amount:
            order_target(stock, 0)
    if winner not in context.portfolio.positions:
        order_target_value(winner, context.portfolio.available_cash * 0.8)
    record(momentum=float(returns[winner]))
