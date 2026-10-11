"""Self-authored strategy from frozen D2 decisions, never reference output replay."""

from decimal import Decimal


def initialize(api):
    api.schedule("10:00", submit)
    api.schedule("10:01", observe)


def submit(api):
    case = api.case
    buy = str(api.day) == str(case.day)
    if not buy and case.end_day == str(case.day):
        return
    quote = api.observe_market("before_submit")
    quantity = case.quantity if buy else -case.quantity
    normal = not quote["suspended"] and Decimal(quote["lower_limit"]) < Decimal(
        quote["last"]
    ) < Decimal(quote["upper_limit"])
    ready = (
        (api.available_cash > Decimal(quote["last"]) * abs(quantity) * Decimal("1.1"))
        if buy
        else api.sellable >= abs(quantity)
    )
    for index in range(case.buy_orders if buy else 1):
        api.snapshot("before_order_" + str(index + 1))
        api.emit(
            "decision",
            action="buy" if buy else "sell",
            quantity=quantity,
            precondition=bool(normal and ready),
        )
        if not (normal and ready):
            api.emit("not_triggered", reason="normal market/cash/sellable precondition failed")
            return
        api.order(quantity)
        api.snapshot("after_order_" + str(index + 1))


def observe(api):
    api.observe_market("next_minute")
    api.snapshot("next_minute")
