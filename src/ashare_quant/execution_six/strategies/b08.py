from ashare_quant.execution_six.strategies.common import observe, prepare


def initialize(ctx):
    ctx.schedule("10:00", submit)
    ctx.schedule("10:01", observe)


def submit(ctx):
    prepare(ctx, "limit", 100)
    ctx.snapshot("after_submit")
