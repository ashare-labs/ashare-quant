"""Trusted original D6 intent, invoked by the native lifecycle."""


def initialize(api):
    for clock in ("09:30", "10:00") if api.case.case_id == "B36" else ("10:00", "10:01"):
        api.schedule(clock, probe)


def probe(api):
    api.callback("run_daily")
    api.snapshot("probe_" + api.clock)
    if api.clock != "10:00":
        return
    if api.case.case_id == "B37":
        api.emit("decision", api="order", security=api.case.security, quantity=100, style="market")
        api.order(100)
        api.snapshot("immediate_after_return")
    elif api.case.case_id in {"B38", "B39"}:
        api.fixed_queries()
        api.snapshot("after_future_request" if api.case.case_id == "B38" else "after_caught_error")
