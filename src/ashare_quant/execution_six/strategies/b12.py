from ashare_quant.execution_six.strategies.common import observe, prepare


def initialize(ctx):
    ctx.schedule("09:20", submit)
    for clock in ("09:25", "09:28", "09:30", "09:31", "09:32"):
        ctx.schedule(clock, checkpoint)
    ctx.schedule("10:01", observe)


def submit(ctx):
    prepare(ctx, "limit", 100, fixed_limit="11.00")
    ctx.snapshot("after_submit")


def checkpoint(ctx):
    ctx.observe_market("opening_checkpoint")
    ctx.snapshot("opening_checkpoint")
    if ctx.clock == "09:28":
        ctx.observe_auction()
