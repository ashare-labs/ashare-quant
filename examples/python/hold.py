"""Project-owned minimal normal callback; no file/network access."""

from ashare_quant.strategy_api import TargetShares


def on_decision(context, data, parameters):
    print("AQ_DECISION", context.day_index, flush=True)
    return TargetShares(100, reason="hold 100 shares")
