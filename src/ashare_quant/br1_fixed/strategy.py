"""Trusted wrapper: native context and bars are never read by strategy selection."""


def init(context):
    subscribe("600000.XSHG")  # noqa: F821


def handle_bar(context, bar_dict):
    from ashare_quant.br1_fixed.mod import dispatch_fixed_intent
    dispatch_fixed_intent()
