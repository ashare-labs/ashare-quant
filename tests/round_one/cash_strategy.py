"""Artificial strategy and prices; no original JQ/real-data reference is loaded."""
# ruff: noqa: F821 -- MODE and RQ strategy APIs are injected by the fixed launcher.

import copy
from datetime import timedelta

from rqalpha.const import POSITION_DIRECTION
from rqalpha.environment import Environment

SID = "600000.XSHG"


def snapshot(context, label):
    account = Environment.get_instance().portfolio.stock_account
    pos = account.get_position(SID, POSITION_DIRECTION.LONG)
    context.probes.append(
        dict(
            kind=label,
            at=context.now.isoformat(),
            **account.cash_projection(),
            quantity=pos.quantity,
            closable=pos.closable,
            receivable=str(pos.dividend_receivable),
            average=str(pos.avg_price),
        )
    )


def init(context):
    subscribe(SID)
    context.trace, context.trades, context.probes = [], [], []
    context.pending = None


def before_trading(context):
    env = Environment.get_instance()
    account = env.portfolio.stock_account
    before = copy.deepcopy(account.get_state())
    account._on_before_trading(None)
    assert account.get_state() == before
    if context.now.day == 4:
        saved_clock = env.calendar_dt, env.trading_dt
        tomorrow = context.now + timedelta(days=1)
        env.update_time(tomorrow, tomorrow)
        try:
            account._on_before_trading(None)
        except RuntimeError as exc:
            assert str(exc) == "CASH_NONADJACENT_OR_UNSETTLED_SESSION"
        else:
            raise AssertionError("UNSETTLED_CROSS_DAY_ACCEPTED")
        finally:
            env.update_time(*saved_clock)
        assert account.get_state() == before
    original = env.data_proxy.get_next_trading_date
    env.data_proxy.get_next_trading_date = lambda current: current
    try:
        account._next_session(context.now.date())
    except RuntimeError as exc:
        assert str(exc) == "CASH_NEXT_SESSION_NOT_ADMITTED"
    else:
        raise AssertionError("CLAMPED_CALENDAR_ACCEPTED")
    finally:
        env.data_proxy.get_next_trading_date = original
    snapshot(context, "before")


def handle_bar(context, bars):
    day, clock = context.now.day, context.now.strftime("%H:%M")
    quantity = 300 if MODE == "partial" else 100
    account = Environment.get_instance().portfolio.stock_account
    if day == 4 and clock == "09:31":
        if MODE == "partial":
            order_shares(SID, quantity, price_or_style=LimitOrder(10))
        else:
            order_shares(SID, quantity)
        snapshot(context, "buy")
    if day == 4 and clock == "09:33":
        assert order_shares(SID, -100) is None
        snapshot(context, "t1_rejected")
    if day == 5 and clock == "09:31" and MODE != "dividend":
        if MODE == "partial":
            order_shares(SID, -quantity, price_or_style=LimitOrder(10))
        else:
            order_shares(SID, -quantity)
        snapshot(context, "sell")
        before = account.get_state()
        try:
            account.deposit_withdraw(-(float(account.transferable_cash) + 1))
        except ValueError:
            assert account.get_state() == before
        else:
            raise AssertionError("WITHDRAWAL_GUARD_MISSING")
    if day == 5 and MODE == "reuse":
        if clock == "09:32":
            context.pending = order_shares(SID, 100, price_or_style=LimitOrder(9))
            snapshot(context, "reserved")
            cancel_order(context.pending)
            snapshot(context, "cancelled")
            # A larger reserve consumes settled cash too; it cannot be withdrawn.
            context.pending = order_shares(SID, 200, price_or_style=LimitOrder(9))
            snapshot(context, "reserved_settled")
            cancel_order(context.pending)
        if clock == "09:33":
            order_shares(SID, 100)
            snapshot(context, "reused")
    snapshot(context, "bar")


def after_trading(context):
    snapshot(context, "after")
