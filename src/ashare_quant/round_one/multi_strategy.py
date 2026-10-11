def initialize(api):
    api.schedule("10:00", probe)
    api.schedule("10:01", probe)


def probe(api):
    api.snapshot("probe")
    if api.clock == "10:00":
        for sid, quantity, label in api.case.order_intents:
            api.transact(sid, quantity, label)
