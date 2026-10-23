"""Migratable JQ-style Python; raw prices and explicit frozen execution costs."""
# ruff: noqa: F405 -- APIs are supplied by the explicit jqdata star import.
from jqdata import *  # noqa: F403


def initialize(context):
    g.stocks = ["600000.XSHG", "000001.XSHE", "000002.XSHE"]
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    run_daily(rebalance, time="open")
    run_daily(report, time="after_close")


def rebalance(context):
    budget = context.portfolio.total_value * 0.9 / len(g.stocks)
    for stock in g.stocks:
        if not get_current_data()[stock].paused:
            order_target_value(stock, budget)


def report(context):
    record(equity=context.portfolio.total_value, cash=context.portfolio.available_cash)
    log.info(context.current_dt, "holdings", len(context.portfolio.positions))
