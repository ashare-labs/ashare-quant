"""Consume AD's hash-bound public calendar artifact; never decode source calendars."""

import hashlib
import importlib.util
import json
from datetime import date, datetime
from pathlib import Path


def attach_calendar(metadata, security, sessions):
    pin = json.loads(Path(__file__).with_name("carry-data-pin.json").read_text())
    binding = Path(pin["binding"])
    if hashlib.sha256(binding.read_bytes()).hexdigest() != pin["binding_sha256"]:
        raise RuntimeError("CALENDAR_BINDING_CHANGED")
    reader = binding.parent / pin["reader"]["path"]
    if hashlib.sha256(reader.read_bytes()).hexdigest() != pin["reader"]["sha256"]:
        raise RuntimeError("CALENDAR_READER_CHANGED")
    spec = importlib.util.spec_from_file_location("ad_bound_calendar_reader", reader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        module.read_calendar(
            binding,
            "carry-march",
            security,
            session,
            role="lifecycle_research",
            expected_binding_sha256=pin["binding_sha256"],
        )
        for session in sessions
    ]
    links = validate_links(rows, security, sessions)
    return {
        **metadata,
        "calendar_links": links,
        "calendar_evidence": dict(
            binding=str(binding),
            binding_sha256=pin["binding_sha256"],
            reader=pin["reader"],
            public_results=rows,
            use="engine logical previous/next session; retrospective research",
            historical_pit=False,
            security_trading_permission=False,
        ),
    }


def validate_links(rows, security, sessions, role="lifecycle_research"):
    if len(rows) != len(sessions) or len(set(sessions)) != len(sessions):
        raise RuntimeError("CALENDAR_SESSION_SCOPE_INVALID")
    result = {}
    for row, session in zip(rows, sessions):
        if (row["security"], row["session_date"], row["role"], row["status"]) != (
            security,
            session,
            role,
            "supported",
        ) or any(
            row[k] is not False
            for k in ("historical_pit", "strategy_visible", "matching_eligible", "network_used")
        ):
            raise RuntimeError("CALENDAR_PUBLIC_ENVELOPE_INVALID")
        previous, current, following = [
            date.fromisoformat(row[k]) for k in ("previous_session", "session_date", "next_session")
        ]
        if not previous < current < following:
            raise RuntimeError("CALENDAR_ADJACENCY_INVALID")
        result[session] = dict(row)
    for first, second in zip(sessions, sessions[1:]):
        if result[first]["next_session"] != second or result[second]["previous_session"] != first:
            raise RuntimeError("CALENDAR_EXECUTION_SESSIONS_NOT_ADJACENT")
    return result


def adjacent(host, dt, field, n=1):
    day = dt.date() if isinstance(dt, datetime) else dt
    row = host.metadata["calendar_links"].get(str(day))
    if n != 1 or row is None or row["status"] != "supported":
        raise RuntimeError("CALENDAR_ENDPOINT_NOT_BOUND")
    return datetime.fromisoformat(row[field])


def callback_previous(host):
    if "calendar_links" not in host.metadata:
        return dict(previous_date=None, previous_date_status="OWNER_ADJACENCY_UNKNOWN")
    return dict(
        previous_date=str(adjacent(host, host.at, "previous_session").date()),
        previous_date_status="OWNER_BOUNDED_CALENDAR",
    )
