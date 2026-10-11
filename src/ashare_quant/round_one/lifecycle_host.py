"""D6 callback/query facade; native engine remains the account owner."""

import json
from pathlib import Path

from .calendar_facts import callback_previous
from .fee_host import FeeAPI, Mod
from .fee_host import Host as FeeHost
from .queries import QueryRefused


class LifecycleAPI(FeeAPI):
    def callback(self, name):
        self.emit(
            "callback",
            name=name,
            state_counter=0,
            **callback_previous(self._host),
        )

    def fixed_queries(self):
        h = self._host
        inputs = json.loads(Path(__file__).with_name("query-inputs.json").read_text())["cases"][
            self.case.case_id
        ]
        for op in inputs["operations"]:
            if h.at.isoformat() != op["as_of"]:
                raise RuntimeError("D6_QUERY_CLOCK_MISMATCH")
            h.emit("query_request", **op)
            request_seq = h.records[-1]["seq"]
            try:
                value = h.provider.queries.query(
                    **{k: op[k] for k in ("api", "parameters", "as_of", "role")}
                )
                h.emit("query_return", request_seq=request_seq, label=op["label"], value=value)
            except QueryRefused as exc:
                h.emit(
                    "query_exception",
                    request_seq=request_seq,
                    label=op["label"],
                    error=exc.to_dict(),
                )


class Host(FeeHost):
    def __init__(self, *args):
        super().__init__(*args)
        self.api = LifecycleAPI(self)

    def before(self):
        self.api.callback("before_trading_start")
        super().before()

    def after(self):
        self.api.callback("after_trading_end")
        super().after()

    def complete(self):
        self.api.callback("on_strategy_end")
        result = super().complete()
        result["previous_date_capability"] = callback_previous(self)["previous_date_status"]
        result["callback_count"] = sum(e["kind"] == "callback" for e in self.records)
        # Actual query rejection must not prevent subsequent callback dispatch.
        if self.case.query_group:
            requests = [e for e in self.records if e["kind"] == "query_request"]
            outputs = [e for e in self.records if e["kind"] in {"query_return", "query_exception"}]
            result["scenario_triggered"] &= len(requests) == 2 and len(outputs) == 2
            result["scenario_triggered"] &= any(
                e["kind"] == "callback" and e["at"] == self.case.at("10:01").isoformat()
                for e in self.records
            )
            result["status"] = (
                "SCENARIO_EXECUTED" if result["scenario_triggered"] else "SCENARIO_NOT_TRIGGERED"
            )
        return result


def load_mod():
    return Mod()
