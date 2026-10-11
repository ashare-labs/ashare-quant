from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from ashare_quant.round_one.calendar_facts import adjacent, callback_previous, validate_links


def rows():
    # Synthetic contract fixture, not a real calendar or reference answer.
    return [
        dict(
            security="SYNTH",
            session_date=d,
            previous_session=p,
            next_session=n,
            role="lifecycle_research",
            status="supported",
            historical_pit=False,
            strategy_visible=False,
            matching_eligible=False,
            network_used=False,
        )
        for p, d, n in [
            ("2024-01-04", "2024-01-05", "2024-01-08"),
            ("2024-01-05", "2024-01-08", "2024-01-09"),
        ]
    ]


def test_calendar_callback_uses_owner_adjacency_and_refuses_outside_endpoint():
    links = validate_links(rows(), "SYNTH", ["2024-01-05", "2024-01-08"])
    host = SimpleNamespace(
        metadata={"calendar_links": links}, at=datetime.fromisoformat("2024-01-08T09:00:00+08:00")
    )
    assert callback_previous(host)["previous_date"] == "2024-01-05"
    with pytest.raises(RuntimeError, match="ENDPOINT_NOT_BOUND"):
        adjacent(host, datetime(2024, 1, 9), "previous_session")
    with pytest.raises(RuntimeError, match="ENDPOINT_NOT_BOUND"):
        adjacent(host, host.at, "previous_session", 2)


def test_calendar_does_not_upgrade_roles_or_accept_clamped_dates():
    original = rows()
    for key, bad in [
        ("previous_session", "2024-01-05"),
        ("role", "strategy"),
        ("historical_pit", True),
        ("security", "OTHER"),
    ]:
        changed = deepcopy(original)
        changed[0][key] = bad
        with pytest.raises(RuntimeError):
            validate_links(changed, "SYNTH", ["2024-01-05", "2024-01-08"])
    changed = deepcopy(original)
    changed[1]["previous_session"] = "2024-01-06"
    with pytest.raises(RuntimeError, match="NOT_ADJACENT"):
        validate_links(changed, "SYNTH", ["2024-01-05", "2024-01-08"])
