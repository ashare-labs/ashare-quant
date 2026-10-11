def initialize(api):
    api.schedule("10:00", probe)
    api.schedule("10:01", probe)


def probe(api):
    label = "price_probe_" + api.clock
    api.observe_market(label)
    if api.clock == "10:00":
        api.observe_market("before_order")
        api.snapshot("before_order")
        api.emit("decision", api="order", security=api.case.security, quantity=100, style="market")
        api.order(100)
        api.snapshot("after_order")
    api.snapshot(label)
