from ashare_quant.execution_six.state import runtime


def init(context):
    runtime["host"].initialize()


def before_trading(context):
    runtime["host"].before()


def handle_bar(context, bar_dict):
    runtime["host"].dispatch()


def after_trading(context):
    runtime["host"].after()
