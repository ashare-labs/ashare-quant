"""Three ordinary target decisions from the fixed logical day index."""

from ashare_quant.strategy_api import NoChange, TargetShares


def on_decision(context, data, parameters):
    print("AQ_DECISION", context.day_index, flush=True)
    if context.day_index == 0:
        return TargetShares(100, reason="first lot")
    if context.day_index == 1 and context.prior_account.quantity == 100:
        return TargetShares(200, reason="second lot")
    if context.day_index == 2:
        return TargetShares(0, reason="close position")
    return NoChange(reason="no further target")
