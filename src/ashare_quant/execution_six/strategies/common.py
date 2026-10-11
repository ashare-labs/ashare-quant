from decimal import Decimal


def prepare(ctx, style, amount, *, suspended=False, fixed_limit=None):
    quote = ctx.observe_market("before_submit")
    ctx.snapshot("before_submit")
    limit = None
    if style == "limit":
        limit = Decimal(fixed_limit) if fixed_limit else min(
            Decimal(quote["upper_limit"]) - Decimal("0.01"),
            (Decimal(quote["last"]) + Decimal("0.20")).quantize(Decimal("0.01")),
        ).quantize(Decimal("0.01"))
    if suspended:
        triggered = quote["suspended"] is True and ctx.available_cash > 10000
    else:
        bound = limit if limit is not None else Decimal(quote["last"])
        triggered = (quote["suspended"] is False
                     and Decimal(quote["lower_limit"]) < bound < Decimal(quote["upper_limit"])
                     and ctx.available_cash > bound * amount)
        if limit is not None and fixed_limit is None:
            triggered = triggered and Decimal(quote["last"]) < limit
    ctx.emit("decision", style=style, amount=amount,
             limit_price=str(limit) if limit is not None else None,
             precondition=bool(triggered))
    if not triggered:
        ctx.emit("not_triggered", reason="frozen scenario precondition failed")
        return None
    return ctx.buy(amount, limit=limit)


def observe(ctx):
    ctx.observe_market("next_minute")
    ctx.snapshot("next_minute")
