"""Original B26: one buy on record day, no sale, daily dividend probes."""


def initialize(api):
    api.schedule("10:00", probe)


def probe(api):
    if str(api.day) == "2024-06-13":
        api.observe_market("before_order")
        api.snapshot("before_order")
        api.emit("decision", api="order", security=api.case.security, quantity=1000, style="market")
        api.request(1000)
        api.snapshot("after_order")
    api.snapshot("dividend_probe")
