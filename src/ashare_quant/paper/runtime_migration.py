"""Explicit source-runtime authorization before a bound holding day starts.

The old continuation object and its identity are archived unchanged. Moving it
out of the old field makes the frozen c5 controller fail its runtime check.
Native account identity, config, plans and checkpoint bytes are never rewritten.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from ashare_quant.contracts import Contract
from ashare_quant.paper.contracts import Sha
from ashare_quant.paper.risk import require
from ashare_quant.storage.artifacts import atomic, digest, lock, regular, sha


def now():
    return datetime.now(timezone.utc)


class Authorization(Contract):
    schema_version: Literal["paper-bound-runtime-authorization.v1"] = (
        "paper-bound-runtime-authorization.v1"
    )
    from_state_sha256: Sha
    account_identity: Sha
    continuation_sha256: Sha
    from_runtime_pin: dict
    to_runtime_pin: dict
    authorized_at: AwareDatetime
    review_sha256: Sha
    reason: str = Field(min_length=1, max_length=1024)
    original_files: dict[str, Sha]


def selected_continuation(state):
    if "calendar_successor" in state:
        require(
            not any(
                k in state
                for k in ("continuation", "archived_continuation", "runtime_authorization")
            ),
            "PAPER_RUNTIME_AUTHORIZATION_SHAPE",
        )
        return state["calendar_successor"]
    migrated = "runtime_authorization" in state
    require(
        ("archived_continuation" in state) == migrated
        and not (migrated and "continuation" in state),
        "PAPER_RUNTIME_AUTHORIZATION_SHAPE",
    )
    return state.get("archived_continuation" if migrated else "continuation")


def authorization_gate(value, m, identity, current_pin, event):
    a = Authorization.model_validate(value)
    require(
        a.account_identity == identity == m.origin_identity
        and a.continuation_sha256 == digest(m)
        and a.from_runtime_pin == m.runtime_pin
        and a.to_runtime_pin == current_pin
        and set(a.to_runtime_pin) == {"sources", "runtime", "rq"}
        and a.to_runtime_pin != a.from_runtime_pin
        and a.to_runtime_pin["runtime"] == a.from_runtime_pin["runtime"]
        and a.to_runtime_pin["rq"] == a.from_runtime_pin["rq"]
        and m.bound_at <= a.authorized_at < m.plan.start_at
        and bool(a.reason.strip()),
        "PAPER_RUNTIME_AUTHORIZATION_CHANGED",
    )
    require(
        isinstance(event, dict)
        and event.get("kind") == "execution_runtime_authorized"
        and event.get("day") == str(m.plan.day)
        and event.get("details") == dict(authorization=digest(a)),
        "PAPER_RUNTIME_AUTHORIZATION_EVENT",
    )
    return a


def original_files(root, day):
    paths = [root / "continuation-origin.json"]
    paths.extend(p for p in sorted((root / day).iterdir()) if p.is_file())
    return {str(p.relative_to(root)): sha(regular(p)) for p in paths}


def authorization_event(state):
    events = [e for e in state["events"] if e.get("kind") == "execution_runtime_authorized"]
    require(len(events) == 1, "PAPER_RUNTIME_AUTHORIZATION_EVENT")
    return events[0]


def verify_authorized_state(root, state, config, m, current_pin):
    from ashare_quant.paper.intraday_continuation import verify_state

    a = authorization_gate(
        state["runtime_authorization"],
        m,
        state["identity"],
        current_pin,
        authorization_event(state),
    )
    raw = regular(root / "runtime-origin.json")
    require(sha(raw) == a.from_state_sha256, "PAPER_RUNTIME_ORIGIN_CHANGED")
    before = json.loads(raw)
    require("runtime_authorization" not in before, "PAPER_RUNTIME_ORIGIN_CHANGED")
    verify_state(root, before, config, a.from_runtime_pin)
    fixed = set(before) - {"continuation", "events", "days", "head", "last_day"}
    require(
        set(state)
        == (set(before) - {"continuation"}) | {"archived_continuation", "runtime_authorization"}
        and all(state[k] == before[k] for k in fixed)
        and state["archived_continuation"] == before["continuation"]
        and before["head"] == m.parent_checkpoint
        and before["last_day"] == str(m.origin_plan.day)
        and set(before["days"]) == {str(m.origin_plan.day)}
        and state["events"][: len(before["events"])] == before["events"]
        and state["events"][len(before["events"]) :][:1] == [authorization_event(state)]
        and a.original_files == original_files(root, str(m.origin_plan.day)),
        "PAPER_RUNTIME_ORIGIN_CHANGED",
    )
    return a


def migrate(
    service,
    *,
    state_sha,
    from_sha,
    to_sha,
    binding_sha,
    review_sha=None,
    reason=None,
    apply=False,
    stage=None,
):
    """Check or atomically authorize one unstarted, already bound continuation."""
    from ashare_quant.paper.intraday_continuation import manifest_gate, verify_state
    from ashare_quant.paper.service import engine_pin

    current = engine_pin()
    require(digest(current) == to_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
    _, config = service._load(verify=False)
    global_lock = (
        Path(tempfile.gettempdir())
        / ("ashare-paper-locks-" + str(os.getuid()))
        / (digest(config.account_id) + ".lock")
    )
    with lock(global_lock), lock(service.root / "writer.lock"):
        raw = regular(service.path)
        state, config = service._load(verify=False)
        m = manifest_gate(selected_continuation(state), config)
        require(digest(m) == binding_sha, "PAPER_RUNTIME_BINDING_CHANGED")
        require(digest(m.runtime_pin) == from_sha, "PAPER_RUNTIME_FROM_CHANGED")
        if "runtime_authorization" in state:
            verify_state(service.root, state, config, current)
            a = Authorization.model_validate(state["runtime_authorization"])
            require(
                a.from_state_sha256 == state_sha
                and (not apply or (a.review_sha256 == review_sha and a.reason == reason)),
                "PAPER_RUNTIME_MIGRATION_CONFLICT",
            )
            return dict(applied=True, unchanged=True, authorization_sha256=digest(a))
        require(sha(raw) == state_sha, "PAPER_RUNTIME_STATE_CHANGED")
        verify_state(service.root, state, config, m.runtime_pin)
        day = str(m.origin_plan.day)
        require(
            set(state["days"]) == {day}
            and state["last_day"] == day
            and state["head"] == m.parent_checkpoint
            and state["days"][day]["status"] == "committed"
            and not (service.root / str(m.plan.day)).exists()
            and m.bound_at <= now() < m.plan.start_at,
            "PAPER_RUNTIME_UNSTARTED_BINDING_REQUIRED",
        )
        require(
            current != m.runtime_pin
            and current["runtime"] == m.runtime_pin["runtime"]
            and current["rq"] == m.runtime_pin["rq"],
            "PAPER_RUNTIME_DEPENDENCIES_CHANGED",
        )
        service._report(state, day, "closing")
        files = original_files(service.root, day)
        if not apply:
            return dict(
                applied=False,
                from_state_sha256=state_sha,
                from_runtime_sha256=from_sha,
                to_runtime_sha256=to_sha,
                continuation_sha256=binding_sha,
                parent_checkpoint=m.parent_checkpoint,
                plan=m.plan.model_dump(mode="json"),
                original_files=files,
            )
        require(review_sha and reason and reason.strip(), "PAPER_RUNTIME_REVIEW_REQUIRED")
        a = Authorization(
            from_state_sha256=state_sha,
            account_identity=state["identity"],
            continuation_sha256=binding_sha,
            from_runtime_pin=m.runtime_pin,
            to_runtime_pin=current,
            authorized_at=now(),
            review_sha256=review_sha,
            reason=reason,
            original_files=files,
        )
        event = dict(
            kind="execution_runtime_authorized",
            day=str(m.plan.day),
            details=dict(authorization=digest(a)),
        )
        authorization_gate(a, m, state["identity"], current, event)
        backup = service.root / "runtime-origin.json"
        if backup.exists():
            require(regular(backup) == raw, "PAPER_RUNTIME_ORIGIN_CHANGED")
        else:
            atomic(backup, raw)
        if stage:
            stage("backup_saved")
        state["archived_continuation"] = state.pop("continuation")
        state["runtime_authorization"] = a.model_dump(mode="json")
        service._save(state, event["kind"], event["day"], **event["details"])
        if stage:
            stage("authorization_saved")
        verify_state(service.root, state, config, current)
        return dict(
            applied=True,
            unchanged=False,
            authorization_sha256=digest(a),
            state_sha256=sha(regular(service.path)),
        )
