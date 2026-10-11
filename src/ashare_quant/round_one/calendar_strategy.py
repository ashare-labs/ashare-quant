"""Original B21 schedules one calendar query and account snapshot each open session."""


def initialize(api):
    api.schedule("10:00", probe)


def probe(api):
    api.calendar_probe()
    api.snapshot("calendar_day")
