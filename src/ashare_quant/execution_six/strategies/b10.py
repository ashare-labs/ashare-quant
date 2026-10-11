from ashare_quant.execution_six.strategies.common import observe, prepare


def initialize(ctx):
    ctx.schedule("10:00", submit)
    ctx.schedule("10:01", observe)


def submit(ctx):
    order = prepare(ctx, "limit", 100000)
    ctx.snapshot("before_cancel")
    partial = order is not None and 0 < order.filled_quantity < order.quantity
    ctx.emit("partial_fill_check", triggered=partial)
    if partial and order.is_active():
        ctx.cancel(order)
    else:
        ctx.emit("not_triggered", reason="no partially filled live remainder")
    ctx.snapshot("after_cancel")
