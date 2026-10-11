"""Original B35 increments state once per configured daily callback."""


def initialize(api):
    api.initialize_state()
    api.schedule("10:00", probe)


def probe(api):
    api.callback("run_daily")
    api.snapshot("probe_" + api.clock)
    api.increment_state()
