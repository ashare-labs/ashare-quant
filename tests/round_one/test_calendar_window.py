"""Synthetic calendar contract tests; these dates are not data evidence."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from ashare_quant.round_one.calendar_cases import CASES
from ashare_quant.round_one.calendar_host import CalendarAPI
from ashare_quant.round_one.calendar_owner import ROLE, bootstrap_window


def fixture():
    value = dict(
        security="SYNTH",
        role=ROLE,
        historical_pit=False,
        strategy_visible=False,
        matching_eligible=False,
        network_used=False,
        synthetic=False,
        calendar=dict(
            status="supported",
            daily_states=[
                dict(
                    calendar_date=d,
                    is_open=o,
                    status="supported",
                    previous_session=p,
                    next_session=n,
                )
                for d, o, p, n in [
                    ("2024-01-05", True, "2024-01-04", "2024-01-08"),
                    ("2024-01-06", False, "2024-01-05", "2024-01-08"),
                    ("2024-01-07", False, "2024-01-05", "2024-01-08"),
                    ("2024-01-08", True, "2024-01-05", "2024-01-09"),
                ]
            ],
        ),
    )
    return value, SimpleNamespace(security="SYNTH", day=date(2024, 1, 5), end_day="2024-01-08")


def test_public_calendar_requires_every_natural_day_and_uses_open_flags():
    value, case = fixture()
    result = bootstrap_window(value, case)
    assert result["calendar"] == ["2024-01-05", "2024-01-08"]
    for mutation in ("missing", "unknown", "wrong-role", "duplicate", "pit"):
        bad = deepcopy(value)
        if mutation == "missing":
            del bad["calendar"]["daily_states"][1]
        elif mutation == "unknown":
            bad["calendar"]["daily_states"][1]["status"] = "unknown"
        elif mutation == "wrong-role":
            bad["role"] = "strategy"
        elif mutation == "duplicate":
            bad["calendar"]["daily_states"].append(bad["calendar"]["daily_states"][0])
        else:
            bad["historical_pit"] = True
        with pytest.raises(RuntimeError):
            bootstrap_window(bad, case)


def test_calendar_plan_comes_from_admitted_sessions_and_forbids_orders():
    plan = CASES["B21"].event_plan({"calendar": ["2024-04-03", "2024-04-05", "2024-04-08"]})
    assert [r["at"][:10] for r in plan if r["kind"] == "before"] == [
        "2024-04-03",
        "2024-04-05",
        "2024-04-08",
    ]
    with pytest.raises(RuntimeError, match="ORDER_DENIED"):
        CalendarAPI(None).order(100)
