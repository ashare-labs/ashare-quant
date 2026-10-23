"""Synthetic oracle exercise. Loaded with RQ's native injected strategy API."""

from rqalpha.const import POSITION_DIRECTION
from rqalpha.environment import Environment

SID = "600000.XSHG"


def scheduled(context, bars):
    context.trace.append(dict(event="SCHEDULED_1455", dt=str(context.now)))


def init(context):
    subscribe(SID)
    context.trace = []
    context.trades = []
    context.probes = []
    context.n = 0
    scheduler.run_daily(scheduled, time_rule=physical_time(14, 55))


def before_trading(context):
    context.trace.append(dict(event="STRATEGY_BEFORE", dt=str(context.now)))


def handle_bar(context, bars):
    context.n += 1
    env = Environment.get_instance()
    context.trace.append(dict(event="STRATEGY_BAR", dt=str(context.now)))
    daily = history_bars(SID, 2, "1d", ["datetime", "close"], include_now=True, adjust_type="none")
    query_dt = history_bars(
        SID, 1, "1d", ["datetime", "close"], include_now=False, adjust_type="none"
    )
    frequency = env.config.base.frequency
    h = history_bars(SID, 2, frequency, ["datetime", "close"], include_now=True, adjust_type="none")
    context.probes.append(
        dict(
            kind="history",
            dt=str(context.now),
            daily=daily["datetime"].tolist(),
            exclude_now=query_dt["datetime"].tolist(),
            own=h["datetime"].tolist(),
            price=env.price_board.get_last_price(SID),
            bar=bars[SID].close,
            as_of=env.data_source.frame().as_of.isoformat(),
            snapshot=env.data_source.frame().snapshot_id,
        )
    )
    if context.n == 1:
        for name in [
            "get_price",
            "index_components",
            "get_fundamentals",
            "get_pit_financials",
            "query",
        ]:
            try:
                globals()[name](SID)
            except RuntimeError as e:
                context.probes.append(dict(kind="blocked", name=name, error=str(e)))
            else:
                raise AssertionError("BYPASS_NOT_BLOCKED:" + name)
        quantity = 300 if MODE in ["semantics", "target_pending", "target_safe"] else 100
        orders = order_to(SID, quantity, style=LimitOrder(10.5))
        context.probes.append(
            dict(kind="target_return", type=type(orders).__name__, count=len(orders))
        )
        if MODE == "target_pending":
            again = order_to(SID, 300, style=LimitOrder(10.5))
            context.probes.append(
                dict(
                    kind="target_again",
                    quantity=[o.quantity for o in again],
                    open=[o.unfilled_quantity for o in get_open_orders()],
                )
            )
        if MODE == "target_safe":
            from ashare_quant.execution.rqalpha.target import target_shares

            again = target_shares(SID, 300, style=LimitOrder(10.5))
            context.probes.append(
                dict(
                    kind="target_safe",
                    quantity=[o.quantity for o in again],
                    open=[o.unfilled_quantity for o in get_open_orders()],
                )
            )
        if frequency == "1d":
            snap = current_snapshot(SID)
            context.probes.append(dict(kind="daily_snapshot", last=snap.last))
    if MODE == "semantics" and context.now.strftime("%H:%M") == "09:33" and context.now.day == 2:
        before = env.portfolio.get_position(SID, POSITION_DIRECTION.LONG).quantity
        orders = order_to(SID, 0, style=LimitOrder(9.5))
        context.probes.append(
            dict(
                kind="tplus_sell",
                before=before,
                result=len(orders),
                after=env.portfolio.get_position(SID, POSITION_DIRECTION.LONG).quantity,
            )
        )
    if MODE == "semantics" and context.now.strftime("%H:%M") == "09:31" and context.now.day == 3:
        order_to(SID, 0, style=LimitOrder(9.5))


def after_trading(context):
    context.trace.append(dict(event="STRATEGY_AFTER", dt=str(context.now)))
