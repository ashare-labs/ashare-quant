"""Native empty-account callbacks driven by AD calendar; queries stay clock gated."""

import json
from pathlib import Path

from .calendar_facts import callback_previous
from .fee_host import FeeAPI
from .fee_host import Host as FeeHost
from .state_host import Host as StateHost
from .state_host import Mod


class CalendarAPI(FeeAPI):
    def order(self, *args, **kwargs):
        raise RuntimeError("CALENDAR_EMPTY_ACCOUNT_ORDER_DENIED")

    def calendar_probe(self):
        h = self._host
        h.require_empty()
        ops = json.loads(Path(__file__).with_name("query-inputs.json").read_text())["cases"]["B21"][
            "operations"
        ]
        matches = [op for op in ops if op["as_of"] == h.at.isoformat()]
        if len(matches) != 1:
            raise RuntimeError("CALENDAR_QUERY_OUTSIDE_FROZEN_CLOCK")
        op = matches[0]
        h.emit("query_request", **op)
        seq = h.records[-1]["seq"]
        value = h.provider.queries.query(
            **{k: op[k] for k in ("api", "parameters", "as_of", "role")}
        )
        h.emit("query_return", request_seq=seq, label=op["label"], value=value)
        h.emit("calendar_probe", **callback_previous(h))


class Host(StateHost):
    def __init__(self, *args):
        FeeHost.__init__(self, *args)
        self.api = CalendarAPI(self)

    def before(self):
        self.require_empty()
        self.emit("day_start", **callback_previous(self))
        FeeHost.before(self)

    def after(self):
        self.require_empty()
        self.snapshot("end_of_day")

    def complete(self):
        self.require_empty()
        result = FeeHost.complete(self)
        days = self.metadata["calendar"]
        starts = [e["at"][:10] for e in self.records if e["kind"] == "day_start"]
        probes = [e["at"][:10] for e in self.records if e["kind"] == "calendar_probe"]
        result.update(
            scenario_triggered=starts == probes == days,
            status="SCENARIO_EXECUTED" if starts == probes == days else "SCENARIO_NOT_TRIGGERED",
            execution_sessions=days,
            calendar_adjacency=self.metadata["calendar_links"],
            holdings_overnight_admitted=False,
            corporate_action_coverage=None,
            scope="empty_account_calendar_only",
        )
        return result


def load_mod():
    return Mod()
