"""Independent-review P3 reproduction: native partial trades/reserve/cancel/withdraw."""
# ruff: noqa: F821 -- native strategy functions injected by the existing fixture.

import copy
from decimal import Decimal

from rqalpha.const import POSITION_DIRECTION
from rqalpha.environment import Environment

SID = "600000.XSHG"


def snapshot(c, label):
    a = Environment.get_instance().portfolio.stock_account
    c.probes.append(dict(kind=label, at=c.now.isoformat(), **a.cash_projection()))


def init(c):
    subscribe(SID)
    c.trace, c.trades, c.probes = [], [], []


def before_trading(c):
    if c.now.day != 8:
        return
    a = Environment.get_instance().portfolio.stock_account
    assert not get_open_orders()
    before = copy.deepcopy(a.get_state())
    try:
        a.deposit_withdraw(Decimal("-994.51"))
    except ValueError:
        pass
    else:
        raise AssertionError("CENT_OVERDRAW_ACCEPTED")
    assert a.get_state() == before
    a.deposit_withdraw(Decimal("-994.50"))
    snapshot(c, "whole_cent_withdrawal")


def handle_bar(c, bars):
    a = Environment.get_instance().portfolio.stock_account
    day, clock = c.now.day, c.now.strftime("%H:%M")
    if day == 4 and clock == "09:31":
        order_shares(SID, 300, price_or_style=LimitOrder(10))
    if day == 5 and clock == "09:31":
        assert a.get_position(SID, POSITION_DIRECTION.LONG).closable == 300
        order_shares(SID, -100)
    if day == 5 and clock == "09:32":
        order_shares(SID, 100)
    if day == 5 and clock == "09:33":
        order_shares(SID, -100)
        order = order_shares(SID, 200, price_or_style=LimitOrder(9))
        a.deposit_withdraw(125)
        a.deposit_withdraw(-a.transferable_cash)
        cancel_order(order)
        a.deposit_withdraw(-a.transferable_cash)
        snapshot(c, "unsettled_only")


def after_trading(c):
    pass
