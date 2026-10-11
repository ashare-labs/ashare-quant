"""Unmodified D5 raw quantity/target intent enters native stock APIs."""

from decimal import Decimal


def initialize(api):
    api.schedule("10:00", probe)
    api.schedule("10:01", probe)


def probe(api):
    api.snapshot("probe")
    if api.clock != "10:00":
        return
    case = api.case
    value = api.total_value * Decimal(case.target_weight) if case.target_weight else case.quantity
    api.transact(value)
