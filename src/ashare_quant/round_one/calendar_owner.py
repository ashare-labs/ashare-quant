"""Bounded public AD calendar facts for an empty account, no raw calendar parser."""

import hashlib
import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

from ashare_quant.diagnostics.runner import digest

from .calendar_facts import validate_links
from .cases import CASES
from .queries import QueryFacade

ROLE = "engine_calendar_retrospective_research"


def bootstrap_window(value, case):
    if (value["security"], value["role"], value["calendar"]["status"]) != (
        case.security,
        ROLE,
        "supported",
    ) or any(
        value[k] is not False
        for k in (
            "historical_pit",
            "strategy_visible",
            "matching_eligible",
            "network_used",
            "synthetic",
        )
    ):
        raise RuntimeError("CALENDAR_PUBLIC_SCOPE_INVALID")
    rows = value["calendar"]["daily_states"]
    by_day = {r["calendar_date"]: r for r in rows}
    if len(by_day) != len(rows):
        raise RuntimeError("CALENDAR_DUPLICATE_DATE")
    days, current = [], case.day
    while str(current) <= case.end_day:
        row = by_day.get(str(current))
        if row is None or row["status"] != "supported" or type(row["is_open"]) is not bool:
            raise RuntimeError("CALENDAR_DAY_NOT_CERTIFIED")
        if row["is_open"]:
            days.append(str(current))
        current += timedelta(days=1)
    if not days:
        raise RuntimeError("CALENDAR_NO_EXECUTION_SESSION")
    links = validate_links(
        [
            dict(
                by_day[day],
                session_date=day,
                security=case.security,
                role=ROLE,
                historical_pit=False,
                strategy_visible=False,
                matching_eligible=False,
                network_used=False,
            )
            for day in days
        ],
        case.security,
        days,
        role=ROLE,
    )
    if date.fromisoformat(days[0]) != case.day or days[-1] != case.end_day:
        raise RuntimeError("CALENDAR_FROZEN_ENDPOINT_CLOSED")
    return dict(
        security=case.security,
        day=str(case.day),
        calendar=days,
        calendar_links=links,
        calendar_evidence=value,
        engine_state_scope="empty_account_no_orders_or_positions",
        corporate_action_coverage=None,
        holdings_overnight_admitted=False,
    )


class CalendarOwner:
    def __init__(
        self, binding, owner_python, case_id, *, query_binding=None, query_owner_python=None
    ):
        if case_id != "B21":
            raise RuntimeError("CALENDAR_FROZEN_CASE_REQUIRED")
        self.case = CASES[case_id]
        self.pin = json.loads(Path(__file__).with_name("calendar-owner-pin.json").read_text())
        if hashlib.sha256(Path(binding).read_bytes()).hexdigest() != self.pin["binding_sha256"]:
            raise RuntimeError("CALENDAR_BINDING_CHANGED")
        self.binding, self.python = str(Path(binding).resolve()), str(Path(owner_python).absolute())
        self.queries = QueryFacade(query_binding, query_owner_python, "calendar")
        self.value = self._read()

    def _read(self):
        root = str(Path(__file__).resolve().parents[1])
        boot = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('ashare_quant',sys.argv[1]+'/__init__.py',"
            "submodule_search_locations=[sys.argv[1]]);"
            "m=importlib.util.module_from_spec(s);sys.modules['ashare_quant']=m;s.loader.exec_module(m);"
            "from ashare_quant.round_one.calendar_worker import main;main()"
        )
        request = dict(operation="calendar")
        env = dict(
            PATH=os.defpath,
            TZ="UTC",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
        )
        run = subprocess.run(
            [self.python, "-I", "-B", "-c", boot, root],
            input=json.dumps(dict(binding=self.binding, query=request)),
            text=True,
            capture_output=True,
            env=env,
            cwd="/",
            timeout=30,
        )
        if run.returncode:
            raise RuntimeError("CALENDAR_PUBLIC_READ_FAILED:" + run.stdout + run.stderr)
        reply = json.loads(run.stdout)
        if (reply["query_sha256"], reply["content_sha256"], reply["network_used"]) != (
            digest(request),
            self.pin["content_sha256"],
            False,
        ) or reply["public_result"]["binding_sha256"] != self.pin["binding_sha256"]:
            raise RuntimeError("CALENDAR_REPLY_IDENTITY_MISMATCH")
        return reply["public_result"]

    def bootstrap(self, case):
        if case != self.case or case.quantity or case.buy_orders:
            raise RuntimeError("CALENDAR_EMPTY_ACCOUNT_ONLY")
        return bootstrap_window(self.value, case)

    def read(self, *args, **kwargs):
        raise RuntimeError("CALENDAR_MARKET_AND_LIFECYCLE_READ_DENIED")

    def descriptor(self):
        return dict(
            evidence_kind="real_public_calendar_observations",
            real_market_data=False,
            dataset_id=None,
            calendar_binding_sha256=self.pin["binding_sha256"],
            owner_pin=self.pin,
            query_provider=self.queries.descriptor(),
            conditions=["retrospective bounded calendar; empty account only"],
            gaps=["historical PIT, prices, lifecycle and cash settlement not certified"],
        )
