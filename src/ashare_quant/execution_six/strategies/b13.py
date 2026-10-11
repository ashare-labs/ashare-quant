from ashare_quant.execution_six.strategies.common import observe, prepare


def initialize(ctx):
    ctx.schedule("10:00", submit)
    ctx.schedule("10:01", observe)


def submit(ctx):
    order = prepare(ctx, "market", 100000)
    ctx.emit("volume_cap_check", triggered=order is not None
             and 0 < order.filled_quantity < order.quantity)
    ctx.snapshot("after_submit")
