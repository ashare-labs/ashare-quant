"""Same bounded callbacks for the saved keep/cancel JQ controls."""

from .common import prepare


def initialize(ctx):
    ctx.order = None
    for clock in ("09:20", "09:24", "09:25", "09:26", "09:28", "09:30", "09:31", "09:32"):
        ctx.schedule(clock, checkpoint)


def checkpoint(ctx):
    ctx.snapshot("checkpoint_" + ctx.clock)
    ctx.emit("strategy_open_query", order_ids=list(ctx.get_open_orders()))
    if ctx.clock == "09:20":
        ctx.order = prepare(ctx, "limit", 100, fixed_limit="11.00")
        ctx.snapshot("after_submit")
    if ctx.clock == "09:28":
        if ctx.cancel_at_checkpoint and ctx.order is not None:
            ctx.cancel(ctx.order)
            ctx.snapshot("after_cancel_09:28")
        ctx.observe_auction()
