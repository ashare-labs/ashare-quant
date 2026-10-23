"""Trusted builtin strategy source injected into RQAlpha (not imported by host)."""
from ashare_quant.execution.rqalpha.target import target_shares

SID = "600000.XSHG"


def init(context):
    subscribe(SID)  # noqa: F821
    context.trace, context.trades, context.probes = [], [], []
    context.engine_events = []
    context.n = 0
    context.entered = False
    context.exited = False


def handle_bar(context, bars):
    context.n += 1
    day = context.now.day
    scenario = SCENARIO  # noqa: F821
    if not context.entered and (scenario != "partial_fill" or (day == 2 and context.now.hour == 15)):
        requested = 300 if scenario == "partial_fill" else 100
        result = target_shares(SID, requested, style=LimitOrder(10.5))  # noqa: F821
        context.probes.append({"kind": "entry", "returned_orders": [str(o.order_id) for o in result],
                               "quantity": requested, "dt": str(context.now)})
        context.entered = True
    if scenario == "t_plus_one" and day == 2 and context.n == 2:
        result = target_shares(SID, 0, style=LimitOrder(10))  # noqa: F821
        context.probes.append({"kind": "same_day_sell", "returned_orders": len(result),
                               "dt": str(context.now)})
    if scenario in {"t_plus_one", "cross_day"} and day == 3 and not context.exited:
        result = target_shares(SID, 0, style=LimitOrder(10 if scenario == "t_plus_one" else 8.5))  # noqa: F821
        context.probes.append({"kind": "next_day_sell", "returned_orders": len(result),
                               "dt": str(context.now)})
        context.exited = True
