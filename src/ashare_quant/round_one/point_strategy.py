"""Original B20 schedule and unconditional 100-share lunch buy; no output replay."""


def initialize(api):
    for clock in ["11:29", "11:30", "12:00", "12:59", "13:00", "13:01", "15:00"]:
        api.schedule(clock, probe)


def probe(api):
    api.emit("callback", name="probe")
    api.snapshot("probe_" + api.clock)
    if api.clock == "12:00":
        api.observe_market("before_order")
        api.snapshot("before_order")
        api.emit("decision", action="buy", quantity=100)
        api.order(100)
        api.snapshot("after_order")
