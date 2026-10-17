"""Partial day-limit orders survive lunch; inventory is sold after native T+1."""
# ruff: noqa: F405 -- APIs are supplied by the explicit jqdata star import.
from jqdata import *  # noqa: F403


def initialize(context):
    g.day = 0
    run_daily(lunch_order, time="11:30")
    run_daily(exit_positions, time="open")


def before_trading_start(context):
    g.day += 1


def lunch_order(context):
    if g.day == 1:
        order("600000.XSHG", 6000, style=LimitOrderStyle(11))


def exit_positions(context):
    if g.day > 1:
        for stock, position in context.portfolio.positions.items():
            if position.closeable_amount:
                order_target(stock, 0)


def after_trading_end(context):
    assert not get_open_orders()
    record(equity=context.portfolio.total_value)
