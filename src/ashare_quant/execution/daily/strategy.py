"""Trusted wrapper; pure strategy sees only its frozen parameters and P scalar."""
def init(context):
    subscribe("600000.XSHG")  # noqa: F821


def before_trading(context):
    from ashare_quant.execution.daily.state import runtime
    runtime["mod"].decide()


def handle_bar(context, bar_dict):
    from ashare_quant.execution.daily.state import runtime
    runtime["mod"].submit()
