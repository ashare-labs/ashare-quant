"""AQ-only control of the three initial requests in gap_probe.py.

Use the original AD research input, initial_cash=10000000, volume_percent=0.0001,
matching=jq_current, cash_policy=resize, sell_tax=0.0005. This intentionally keeps
AQ's strict shared minute budget and is NOT a claim that the JQ probe is ported.
"""


def initialize(context):
    g.day = 0
    run_daily(initial_requests, "09:40")


def before_trading_start(context):
    g.day += 1


def initial_requests(context):
    if g.day == 1:
        order("600000.XSHG", 100000)
        order("600000.XSHG", 100000)
        order("000001.XSHE", 100000)
