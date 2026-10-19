"""Explicit adoption of a completely settled v3 ledger by a reviewed runtime.

Genesis identity, configuration, history and native checkpoint bytes stay fixed.
This authorization grants reading/recovery of committed phases, not a new day.
Subsequent holding days require a separate v3 successor binding.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from ashare_quant.contracts import Contract
from ashare_quant.paper.contracts import Sha
from ashare_quant.paper.intraday_contracts import REBALANCE_MODEL
from ashare_quant.paper.risk import require
from ashare_quant.storage.artifacts import atomic, digest, lock, regular, sha

FIELD = "settled_runtime_authorization"
# Other dependency transitions need their own native compatibility validation.
DATA_TRANSITIONS = {("0.8.0.dev2", "0.10.0.dev5")}


def now():
    return datetime.now(timezone.utc)


class Authorization(Contract):
    schema_version: Literal["paper-settled-v3-runtime-authorization.v1"] = (
        "paper-settled-v3-runtime-authorization.v1"
    )
    account_identity: Sha
    from_state_sha256: Sha
    from_runtime_pin: dict
    to_runtime_pin: dict
    owner_content_sha256: Sha
    owner_reader_sha256: Sha
    last_day: str
    parent_checkpoint: Sha
    parent_report_sha256: Sha
    original_files: dict[str, Sha]
    authorized_at: AwareDatetime
    review_sha256: Sha
    reason: str = Field(min_length=1, max_length=1024)


def runtime_transition(before, after):
    """Record AD's actual version change; keep Python, native ABI and RQ exact."""
    require(
        set(before) == set(after) == {"sources", "runtime", "rq"}
        and before["rq"] == after["rq"]
        and set(before["runtime"]) == set(after["runtime"])
        and "ashare-data" in before["runtime"]
        and all(
            before["runtime"][k] == after["runtime"][k]
            for k in before["runtime"]
            if k != "ashare-data"
        ),
        "PAPER_RUNTIME_DEPENDENCIES_CHANGED",
    )
    pair = before["runtime"]["ashare-data"], after["runtime"]["ashare-data"]
    require(pair[0] == pair[1] or pair in DATA_TRANSITIONS, "PAPER_DATA_TRANSITION_UNREVIEWED")


def authorization_gate(value, identity, origin_pin, current_pin):
    a = Authorization.model_validate(value)
    runtime_transition(a.from_runtime_pin, a.to_runtime_pin)
    require(
        a.account_identity == identity
        and a.from_runtime_pin == origin_pin
        and a.to_runtime_pin == current_pin
        and a.from_runtime_pin != a.to_runtime_pin
        and bool(a.original_files)
        and bool(a.reason.strip()),
        "PAPER_SETTLED_AUTHORIZATION_CHANGED",
    )
    return a


def installed_owner():
    from importlib.resources import files

    from ashare_quant.paper.online_adapter import owner_content_identity
    from ashare_quant.providers.runtime import verify

    return digest(verify()), owner_content_identity(Path(str(files("ashare_data"))).parent)


def closed_state(service, state, config):
    """Reconcile every published phase with its actual native checkpoint."""
    require(
        config.model == REBALANCE_MODEL
        and bool(state["days"])
        and state["last_day"] == max(state["days"])
        and all(r["status"] == "committed" for r in state["days"].values())
        and not any(
            k in state
            for k in (
                "continuation",
                "archived_continuation",
                "runtime_authorization",
                "calendar_successor",
            )
        ),
        "PAPER_SETTLED_V3_REQUIRED",
    )
    report = None
    for index, day in enumerate(sorted(state["days"]), 1):
        report = service._report(state, day, "closing")
        native = report["native"]
        require(
            native["settlement_count"] == index
            and native["cursor"] == 4 * index
            and native["account"]["open_orders"] == []
            and Decimal(native["account"]["frozen_cash"]) == 0,
            "PAPER_SETTLED_NATIVE_PARENT",
        )
    require(report["checkpoint_sha256"] == state["head"], "PAPER_SETTLED_NATIVE_PARENT")
    return report


def verify_state(root, state, config, current_pin):
    from ashare_quant.paper.online import OnlineService

    a = authorization_gate(state[FIELD], state["identity"], state["engine_pin"], current_pin)
    content, owner = installed_owner()
    require(
        (a.owner_content_sha256, a.owner_reader_sha256) == (content, owner),
        "PAPER_SETTLED_OWNER_CHANGED",
    )
    raw = regular(root / "runtime-origins" / (a.from_state_sha256 + ".json"))
    require(sha(raw) == a.from_state_sha256, "PAPER_SETTLED_ORIGIN_CHANGED")
    before = json.loads(raw)
    require(
        FIELD not in before
        and set(state) == set(before) | {FIELD}
        and all(state[k] == before[k] for k in before if k != "events")
        and state["events"][: len(before["events"])] == before["events"]
        and len(state["events"]) == len(before["events"]) + 1,
        "PAPER_SETTLED_ORIGIN_CHANGED",
    )
    event = state["events"][-1]
    require(
        event.get("kind") == "settled_runtime_authorized"
        and event.get("day") == a.last_day
        and event.get("details") == {"authorization": digest(a)},
        "PAPER_SETTLED_AUTHORIZATION_EVENT",
    )
    for name, expected in a.original_files.items():
        relative = Path(name)
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and sha(regular(root / relative)) == expected,
            "PAPER_SETTLED_ARTIFACT_CHANGED",
        )
    report = closed_state(OnlineService(root), before, config)
    require(
        a.last_day == before["last_day"]
        and a.parent_checkpoint == before["head"]
        and a.parent_report_sha256
        == before["days"][a.last_day]["reports"]["closing"]["report_sha256"]
        and report["checkpoint_sha256"] == a.parent_checkpoint
        and datetime.fromisoformat(before["days"][a.last_day]["close"]["received_at"])
        <= a.authorized_at,
        "PAPER_SETTLED_NATIVE_PARENT",
    )
    return a


def migrate(
    service, *, state_sha, from_sha, to_sha, apply=False, review_sha=None, reason=None, stage=None
):
    from ashare_quant.paper.calendar_successor import frozen_files
    from ashare_quant.paper.service import engine_pin

    current = engine_pin()
    require(digest(current) == to_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
    _, config = service._load(verify=False)
    serial = (
        Path(tempfile.gettempdir())
        / ("ashare-paper-locks-" + str(os.getuid()))
        / (digest(config.account_id) + ".lock")
    )
    with lock(serial), lock(service.root / "writer.lock"):
        raw = regular(service.path)
        state, config = service._load(verify=False)
        if FIELD in state:
            a = verify_state(service.root, state, config, current)
            require(
                a.from_state_sha256 == state_sha
                and digest(a.from_runtime_pin) == from_sha
                and (not apply or (a.review_sha256 == review_sha and a.reason == reason)),
                "PAPER_SETTLED_MIGRATION_CONFLICT",
            )
            return dict(applied=True, unchanged=True, authorization_sha256=digest(a))
        require(sha(raw) == state_sha, "PAPER_RUNTIME_STATE_CHANGED")
        require(digest(state["engine_pin"]) == from_sha, "PAPER_RUNTIME_FROM_CHANGED")
        runtime_transition(state["engine_pin"], current)
        require(state["engine_pin"] != current, "PAPER_SETTLED_RUNTIME_UNCHANGED")
        closed_state(service, state, config)
        at = now()
        day = state["last_day"]
        require(
            datetime.fromisoformat(state["days"][day]["close"]["received_at"]) <= at,
            "PAPER_CONTINUATION_CLOSE_NOT_VISIBLE",
        )
        content, owner = installed_owner()
        original = frozen_files(service.root)
        # A previous interrupted attempt may have saved only this exact origin.
        origin_name = "runtime-origins/" + state_sha + ".json"
        original.pop(origin_name, None)
        result = dict(
            applied=False,
            account_identity=state["identity"],
            from_state_sha256=state_sha,
            from_runtime_pin=state["engine_pin"],
            to_runtime_pin=current,
            owner_content_sha256=content,
            owner_reader_sha256=owner,
            parent_checkpoint=state["head"],
            parent_report_sha256=state["days"][day]["reports"]["closing"]["report_sha256"],
            original_files=original,
            permits_new_day=False,
        )
        if not apply:
            return result
        require(review_sha and reason and reason.strip(), "PAPER_RUNTIME_REVIEW_REQUIRED")
        a = Authorization(
            account_identity=state["identity"],
            from_state_sha256=state_sha,
            from_runtime_pin=state["engine_pin"],
            to_runtime_pin=current,
            owner_content_sha256=content,
            owner_reader_sha256=owner,
            last_day=day,
            parent_checkpoint=state["head"],
            parent_report_sha256=result["parent_report_sha256"],
            original_files=original,
            authorized_at=at,
            review_sha256=review_sha,
            reason=reason,
        )
        authorization_gate(a, state["identity"], state["engine_pin"], current)
        origin = service.root / origin_name
        origin.parent.mkdir(exist_ok=True)
        if origin.exists():
            require(regular(origin) == raw, "PAPER_SETTLED_ORIGIN_CHANGED")
        else:
            atomic(origin, raw)
        if stage:
            stage("origin_saved")
        state[FIELD] = a.model_dump(mode="json")
        service._save(state, "settled_runtime_authorized", day, authorization=digest(a))
        if stage:
            stage("authorization_saved")
        verify_state(service.root, state, config, current)
        return dict(
            applied=True,
            unchanged=False,
            authorization_sha256=digest(a),
            state_sha256=sha(regular(service.path)),
            permits_new_day=False,
        )
