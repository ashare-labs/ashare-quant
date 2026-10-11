"""Unchanged original decisions through a bounded, trusted strategy facade."""

from decimal import Decimal


def initialize(api):
    api.schedule("10:00", probe)
    api.schedule("10:01", probe)


def probe(api):
    case, today = api.case, str(api.day)
    if case.case_id in {"B14", "B16", "B17", "B18"}:
        if api.clock == "10:01":
            api.observe_market("next_minute")
            api.snapshot("next_minute")
            return
        buy = today == "2024-03-04"
        quote = api.observe_market("before_submit")
        quantity = case.quantity if buy else -case.quantity
        normal = not quote["suspended"] and Decimal(quote["lower_limit"]) < Decimal(
            quote["last"]
        ) < Decimal(quote["upper_limit"])
        ready = (
            api.available_cash > Decimal(quote["last"]) * abs(quantity) * Decimal("1.1")
            if buy
            else api.sellable >= abs(quantity)
        )
        api.snapshot("before_order_1")
        api.emit(
            "decision",
            action="buy" if buy else "sell",
            quantity=quantity,
            precondition=bool(normal and ready),
        )
        if not (normal and ready):
            api.emit("not_triggered", reason="normal market/cash/sellable precondition failed")
            return
        api.request(quantity)
        api.snapshot("after_order_1")
        return
    if case.case_id == "B22":
        api.emit("callback", name="probe")
        api.snapshot("probe_" + api.clock)
        if api.clock != "10:00" or today == "2024-03-06":
            return
        if today == "2024-03-05":
            api.emit("sell_precondition", closeable_amount=api.sellable)
            if api.sellable < case.quantity:
                api.emit("not_triggered", reason="sellable amount below 10000")
                return
        api.observe_market("before_order")
        api.snapshot("before_order")
        api.request(case.quantity if today == "2024-03-04" else -case.quantity)
        api.snapshot("after_order")
        return
    api.snapshot("probe")
    if api.clock != "10:00":
        return
    requests = (
        [(case.quantity, "buy_day_one"), (-case.quantity, "sell_same_callback")]
        if today == "2024-03-04"
        else [(-case.quantity, "sell_next_day")]
    )
    for quantity, label in requests:
        # Intentionally do not pre-check or crop the same-day sell request.
        api.snapshot(label + "_before")
        api.request(quantity, label)
        api.snapshot(label + "_after")
