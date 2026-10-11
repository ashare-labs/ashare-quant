"""Trusted native wrapper; user signal never receives the native environment."""


def init(context):
    from ashare_quant.daily_portfolio.native import runtime

    subscribe(list(runtime["data"].ids))  # noqa: F821


def before_trading(context):
    from ashare_quant.daily_portfolio.native import runtime

    runtime["mod"].decide()


def handle_bar(context, bar_dict):
    from ashare_quant.daily_portfolio.native import runtime

    runtime["mod"].submit()
