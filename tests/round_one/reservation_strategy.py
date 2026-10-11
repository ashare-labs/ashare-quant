"""Synthetic native oracle: unrelated price/quantity/date, no JQ answer import."""

# ruff: noqa: F821 -- injected native strategy functions.
import copy

from rqalpha.environment import Environment

SID = "600000.XSHG"


def snap(context, label):
    account = Environment.get_instance().portfolio.stock_account
    context.probes.append(
        dict(
            kind=label,
            at=context.now.isoformat(),
            cash=account.cash,
            total_cash=account.total_cash,
            frozen_cash=account.frozen_cash,
            account_state=copy.deepcopy(account.get_state()),
        )
    )


def init(context):
    subscribe(SID)
    context.trace, context.trades, context.probes = [], [], []
    context.pending = None


def handle_bar(context, bars):
    clock = context.now.strftime("%H:%M")
    if clock == "09:31":
        order = order_shares(
            SID, 100 if MODE == "full_fill" else 700, price_or_style=LimitOrder(10.7)
        )
        assert order.filled_quantity == 100
        context.pending = order.order_id
        snap(context, "partial")
    if clock == "09:33" and MODE == "second_order":
        order_shares(SID, 700, price_or_style=LimitOrder(10.7))
