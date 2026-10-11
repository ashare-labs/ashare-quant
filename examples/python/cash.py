"""Use only the previous committed native cash snapshot."""

from decimal import Decimal

from ashare_quant.strategy_api import NoChange, TargetShares


def on_decision(context, data, parameters):
    print("AQ_DECISION", context.day_index, flush=True)
    if context.prior_account.cash >= Decimal("1300"):
        return TargetShares(100, reason="cash entry")
    if context.prior_account.cash < Decimal("300"):
        return TargetShares(0, reason="cash exit")
    return NoChange(reason="hold current position")
