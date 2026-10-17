"""Synthetic strategy, not a real source run."""

# ruff: noqa: F821
from rqalpha.environment import Environment

SID = "600000.XSHG"


def init(context):
    subscribe(SID)
    context.trace, context.trades, context.probes = [], [], []


def handle_bar(context, bars):
    account = Environment.get_instance().portfolio.stock_account
    clock = context.now.strftime("%H:%M")
    if clock == "09:31":
        quantity = 700 if MODE == "multiple" else 100
        limit = 10 if MODE == "fee_rejection" else 10.7
        result = order_shares(SID, quantity, price_or_style=LimitOrder(limit))
        if MODE == "fee_rejection":
            assert result is None
        elif MODE == "multiple":
            order_shares(SID, 100, price_or_style=LimitOrder(9))
    if clock in ("09:31", "09:32", "09:33"):
        context.probes.append(
            dict(
                kind=clock, cash=account.cash, frozen=account.frozen_cash, total=account.total_cash
            )
        )
