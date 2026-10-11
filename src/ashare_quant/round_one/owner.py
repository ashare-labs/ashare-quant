"""New binding profile; old six-case source/runtime pins remain immutable."""

import json
from pathlib import Path

from ashare_quant.execution_six.owner import PublicOwner as SixOwner

from .cases import CASES


class PublicOwner(SixOwner):
    cases = CASES
    dataset_pin_path = Path(__file__).with_name("single-day-dataset-pin.json")
    worker_module = "ashare_quant.round_one.owner_worker"

    @staticmethod
    def get_pin():
        return json.loads(Path(__file__).with_name("owner-pin.json").read_text())

    def __init__(
        self, binding, owner_python, case_id, *, query_binding=None, query_owner_python=None
    ):
        super().__init__(binding, owner_python, case_id)
        self.queries = None
        if getattr(self.case, "query_group", None):
            if not query_binding or not query_owner_python:
                raise RuntimeError("ROUND_ONE_QUERY_BINDING_REQUIRED")
            from .queries import QueryFacade

            self.queries = QueryFacade(query_binding, query_owner_python, self.case.query_group)

    def descriptor(self):
        value = super().descriptor()
        if self.queries is not None:
            value["query_provider"] = self.queries.descriptor()
        return value

    def validate_scope(self):
        # Market observations are reusable by security and clock. The source case
        # provenance remains unchanged and never becomes this case's result.
        scope = [self.case.security, str(self.case.day)]
        if scope not in self.binding["case_scope"].values():
            raise RuntimeError("ROUND_ONE_DATA_SECURITY_DATE_NOT_IN_BOUND_SCOPE")

    def bootstrap(self, case):
        if case.end_day != str(case.day):
            raise RuntimeError("ROUND_ONE_OVERNIGHT_CALENDAR_ACTION_COVERAGE_NOT_ADMITTED")
        value = super().bootstrap(case)
        if case.case_id in {"B36", "B37", "B38", "B39"}:
            from .calendar_facts import attach_calendar

            value = attach_calendar(value, case.security, [str(case.day)])
        return value
