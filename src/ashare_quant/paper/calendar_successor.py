"""Append a reviewed holding session to any completely settled native account.

Each binding archives the entire prior control state and freezes every existing
account artifact. Trading dates come from the owner calendar, never date + 1.
Source/owner changes are explicit in this binding; the genesis identity stays.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

import ashare_quant.portfolio.signals as signals
from ashare_quant.contracts import Contract
from ashare_quant.paper.contracts import Sha
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.risk import require
from ashare_quant.storage.artifacts import atomic, digest, lock, regular, sha

SCHEMA = "paper-calendar-hold-successor.v2"
RETIRED = {"continuation", "archived_continuation", "runtime_authorization", "calendar_successor"}


def now():
    return datetime.now(timezone.utc)


class Successor(Contract):
    schema_version: Literal["paper-calendar-hold-successor.v2"] = SCHEMA
    origin_identity: Sha
    origin_engine_pin: dict
    from_runtime_pin: dict
    runtime_pin: dict
    origin_state_sha256: Sha
    parent_checkpoint: Sha
    parent_report_sha256: Sha
    parent_cursor: int = Field(strict=True, ge=4)
    parent_settlement_count: int = Field(strict=True, ge=1)
    bound_at: AwareDatetime
    review_sha256: Sha
    genesis_plan: IntradayPlan
    origin_plan: IntradayPlan
    plan: IntradayPlan
    hold_targets: dict[str, int] = Field(min_length=3, max_length=3)
    action_baseline_lineage: tuple[Sha, ...] = Field(min_length=1)
    original_files: dict[str, Sha]

    @model_validator(mode="after")
    def bounded(self):
        a, b = self.origin_plan, self.plan
        mutable = {
            "day",
            "start_at",
            "intent_deadline",
            "execution_deadline",
            "daily_capture",
            "admission_reader_sha256",
            "action_baseline_sha256",
        }
        require(
            a.model == b.model == "prior-close-intent-minute-partial-cancel.v2"
            and b.day > a.day
            and self.bound_at < b.start_at
            and self.genesis_plan.day <= a.day
            and {k: v for k, v in a.model_dump().items() if k not in mutable}
            == {k: v for k, v in b.model_dump().items() if k not in mutable}
            and set(self.hold_targets) == set(b.universe)
            and all(type(q) is int and q >= 0 for q in self.hold_targets.values())
            and self.parent_cursor == 4 * self.parent_settlement_count
            and self.action_baseline_lineage[-1] == b.action_baseline_sha256
            and a.action_baseline_sha256 in self.action_baseline_lineage
            and self.genesis_plan.action_baseline_sha256 in self.action_baseline_lineage
            and bool(self.original_files),
            "PAPER_SUCCESSOR_SCOPE",
        )
        for pin in (self.origin_engine_pin, self.from_runtime_pin, self.runtime_pin):
            require(
                set(pin) == {"sources", "runtime", "rq"}
                and pin["runtime"] == self.origin_engine_pin["runtime"]
                and pin["rq"] == self.origin_engine_pin["rq"],
                "PAPER_RUNTIME_DEPENDENCIES_CHANGED",
            )
        return self


def manifest_gate(value, config, decision=None):
    m = Successor.model_validate(value)
    require(
        digest(m.genesis_plan) == config.scenario_manifest_sha256
        and m.genesis_plan.admission_reader_sha256 == config.admission_reader_sha256
        and m.plan.model == config.model
        and m.plan.universe == config.universe
        and m.plan.initial_cash == config.initial_cash,
        "PAPER_SUCCESSOR_IDENTITY",
    )
    if decision is not None:
        r = decision.strategy_receipt or {}
        require(
            decision.trading_date == m.plan.day
            and decision.previous_open == m.origin_plan.day
            and decision.expected_parent == m.parent_checkpoint
            and decision.targets == m.hold_targets
            and r.get("scheduled") is False
            and r.get("signal_capture") == m.plan.daily_capture
            and r.get("equity_from") == "previous_native_checkpoint"
            and r.get("intraday_plan") == m.plan.model_dump(mode="json"),
            "PAPER_CONTINUATION_HOLD_ONLY",
        )
    return m


def current_authority(state):
    from ashare_quant.paper.runtime_migration import selected_continuation

    m = selected_continuation(state)
    if "runtime_authorization" in state:
        return state["runtime_authorization"]["to_runtime_pin"]
    return m["runtime_pin"] if m is not None else state["engine_pin"]


def verify_previous(root, state, config):
    from ashare_quant.paper.intraday_continuation import verify_state as verify
    from ashare_quant.paper.runtime_migration import selected_continuation

    pin = current_authority(state)
    if selected_continuation(state) is not None:
        verify(root, state, config, pin)
    else:
        require(pin == state["engine_pin"], "PAPER_RUNTIME_CHANGED")
    return pin


def frozen_files(root):
    return {
        str(p.relative_to(root)): sha(regular(p))
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and p != root / "state.json"
        and p.name != "writer.lock"
        and not p.name.endswith(".pending")
    }


def verify_state(root, state, config, current_pin):
    from ashare_quant.paper.intraday_continuation import verify_binding
    from ashare_quant.paper.online import OnlineService

    m = manifest_gate(state["calendar_successor"], config)
    require(current_pin == m.runtime_pin, "PAPER_SUCCESSOR_RUNTIME_CHANGED")
    raw = regular(root / "successor-origins" / (m.origin_state_sha256 + ".json"))
    require(sha(raw) == m.origin_state_sha256, "PAPER_SUCCESSOR_ORIGIN_CHANGED")
    before = json.loads(raw)
    require(
        verify_previous(root, before, config) == m.from_runtime_pin,
        "PAPER_SUCCESSOR_ORIGIN_CHANGED",
    )
    fixed = set(before) - RETIRED - {"events", "days", "head", "last_day"}
    day = str(m.origin_plan.day)
    require(
        set(state) == (set(before) - RETIRED) | {"calendar_successor"}
        and all(state[k] == before[k] for k in fixed)
        and state["identity"] == before["identity"] == m.origin_identity
        and state["engine_pin"] == before["engine_pin"] == m.origin_engine_pin
        and before["head"] == m.parent_checkpoint
        and before["last_day"] == day
        and set(state["days"]) <= set(before["days"]) | {str(m.plan.day)}
        and all(state["days"].get(d) == row for d, row in before["days"].items())
        and state["events"][: len(before["events"])] == before["events"],
        "PAPER_SUCCESSOR_ORIGIN_CHANGED",
    )
    tail = state["events"][len(before["events"]) :]
    require(
        bool(tail) and sum(e.get("kind") == "holding_successor_bound" for e in tail) == 1,
        "PAPER_CONTINUATION_BINDING_CHANGED",
    )
    verify_binding(m, tail[0])
    for name, expected in m.original_files.items():
        relative = Path(name)
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and sha(regular(root / relative)) == expected,
            "PAPER_SUCCESSOR_ARCHIVE_CHANGED",
        )
    require(
        m.original_files == {name: sha(regular(root / name)) for name in m.original_files},
        "PAPER_SUCCESSOR_ARCHIVE_CHANGED",
    )
    report = OnlineService(root)._report(before, day, "closing")
    row = before["days"][day]
    require(
        all(r["status"] == "committed" for r in before["days"].values())
        and len(before["days"]) == m.parent_settlement_count
        and report["native"]["cursor"] == m.parent_cursor
        and report["native"]["settlement_count"] == m.parent_settlement_count
        and report["checkpoint_sha256"] == m.parent_checkpoint
        and sha(regular(root / day / "closing-report.json")) == m.parent_report_sha256
        and row["decision"]["next_open"] == str(m.plan.day)
        and row["close"]["data_manifest_sha256"] == m.plan.daily_capture
        and IntradayPlan.model_validate(row["decision"]["strategy_receipt"]["intraday_plan"])
        == m.origin_plan
        and report["native"]["account"]["open_orders"] == []
        and m.hold_targets
        == {s: report["native"]["account"]["positions"][s]["quantity"] for s in config.universe},
        "PAPER_SUCCESSOR_NATIVE_PARENT",
    )
    return m


def verify_request(request, config, decision, current_pin):
    from ashare_quant.paper.intraday_continuation import verify_binding

    m = manifest_gate(request["continuation"], config, decision)
    verify_binding(m, request.get("continuation_binding"))
    require(
        "runtime_authorization" not in request
        and current_pin == m.runtime_pin
        and request["engine_pin"] == m.origin_engine_pin
        and request["identity"] == m.origin_identity
        and request["parent_sha"] == m.parent_checkpoint
        and request["parent_complete"] is True
        and request["base_cursor"] == m.parent_cursor
        and decision.strategy_receipt.get("continuation") == m.model_dump(mode="json"),
        "PAPER_SUCCESSOR_RUNTIME_CHANGED",
    )


def reader_config(config, plan):
    """Use the explicitly bound owner without rewriting the genesis identity."""
    return config.model_copy(update={"admission_reader_sha256": plan.admission_reader_sha256})


def bound_reader_config(state, config, day):
    if "calendar_successor" in state:
        m = manifest_gate(state["calendar_successor"], config)
        if str(m.plan.day) == str(day):
            return reader_config(config, m.plan)
    return config


def build_plan(service, reader, start_at):
    state, config = service._load(verify=False)
    verify_previous(service.root, state, config)
    day = state["last_day"]
    require(
        day is not None and all(r["status"] == "committed" for r in state["days"].values()),
        "PAPER_CONTINUATION_CLOSE_REQUIRED",
    )
    service._report(state, day, "closing")
    row = state["days"][day]
    owned = reader.reviewed_successor()
    start = datetime.fromisoformat(start_at) if isinstance(start_at, str) else start_at
    return IntradayPlan.model_validate(
        {
            **row["decision"]["strategy_receipt"]["intraday_plan"],
            "day": row["decision"]["next_open"],
            "start_at": start,
            "intent_deadline": start + timedelta(minutes=5),
            "execution_deadline": start + timedelta(minutes=15),
            "daily_capture": row["close"]["data_manifest_sha256"],
            "admission_reader_sha256": owned["admission_reader_sha256"],
            "action_baseline_sha256": owned["action_baseline_sha256"],
        }
    )


def bind(
    service, reader, plan, *, state_sha, from_runtime_sha, runtime_sha, review_sha, stage=None
):
    from ashare_quant.paper.service import engine_pin

    current = engine_pin()
    require(digest(current) == runtime_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
    plan = IntradayPlan.model_validate(plan)
    _, config = service._load(verify=False)
    global_lock = (
        Path(tempfile.gettempdir())
        / ("ashare-paper-locks-" + str(os.getuid()))
        / (digest(config.account_id) + ".lock")
    )
    with lock(global_lock), lock(service.root / "writer.lock"):
        raw = regular(service.path)
        state, config = service._load(verify=False)
        if "calendar_successor" in state and state["calendar_successor"]["plan"]["day"] == str(
            plan.day
        ):
            m = verify_state(service.root, state, config, current)
            require(
                m.origin_state_sha256 == state_sha
                and m.plan == plan
                and digest(m.from_runtime_pin) == from_runtime_sha
                and m.review_sha256 == review_sha,
                "PAPER_CONTINUATION_IDEMPOTENCY_CONFLICT",
            )
            return state
        require(sha(raw) == state_sha, "PAPER_CONTINUATION_STATE_CHANGED")
        prior_pin = verify_previous(service.root, state, config)
        require(digest(prior_pin) == from_runtime_sha, "PAPER_SUCCESSOR_FROM_RUNTIME")
        day = state["last_day"]
        require(
            day is not None and all(r["status"] == "committed" for r in state["days"].values()),
            "PAPER_CONTINUATION_CLOSE_REQUIRED",
        )
        previous = service._report(state, day, "closing")
        row = state["days"][day]
        owned = reader.reviewed_successor()
        packet = reader.read(reader_config(config, plan), "daily", plan.daily_capture)
        opens = packet["calendar"]["open_days"]
        next_open = next((d for d in opens if d > day), None)
        require(
            opens == sorted(set(opens))
            and day in opens
            and str(plan.day)
            == row["decision"]["next_open"]
            == packet["calendar"]["next_open"]
            == next_open
            and packet["signal_day"] == day
            and packet["signal_ready"]
            and not packet["revisions"]
            and row["close"]["data_manifest_sha256"] == plan.daily_capture
            and owned["first_window"][0]
            <= min(state["days"])
            <= day
            < str(plan.day)
            <= owned["first_window"][1]
            and owned["action_baseline_sha256"] == plan.action_baseline_sha256
            and owned["admission_reader_sha256"] == plan.admission_reader_sha256,
            "PAPER_CONTINUATION_OWNER_SCOPE",
        )
        require(
            not signals.scheduled("equal-weight", str(plan.day), day, False),
            "PAPER_CONTINUATION_REBALANCE_REQUIRED",
        )
        account, n = previous["native"]["account"], previous["native"]
        require(
            account["open_orders"] == []
            and n["settlement_count"] == len(state["days"])
            and n["cursor"] == 4 * len(state["days"]),
            "PAPER_SUCCESSOR_NATIVE_PARENT",
        )
        at = now()
        require(
            datetime.fromisoformat(row["close"]["received_at"]) <= at
            and datetime.fromisoformat(packet["completed_at"]) <= at
            and datetime.fromisoformat(owned["reviewed_at"]) <= at,
            "PAPER_CONTINUATION_CLOSE_NOT_VISIBLE",
        )
        first = state["days"][min(state["days"])]
        m = Successor(
            origin_identity=state["identity"],
            origin_engine_pin=state["engine_pin"],
            from_runtime_pin=prior_pin,
            runtime_pin=current,
            origin_state_sha256=sha(raw),
            parent_checkpoint=state["head"],
            parent_report_sha256=row["reports"]["closing"]["report_sha256"],
            parent_cursor=n["cursor"],
            parent_settlement_count=n["settlement_count"],
            bound_at=at,
            review_sha256=review_sha,
            genesis_plan=first["decision"]["strategy_receipt"]["intraday_plan"],
            origin_plan=row["decision"]["strategy_receipt"]["intraday_plan"],
            plan=plan,
            hold_targets={s: account["positions"][s]["quantity"] for s in config.universe},
            action_baseline_lineage=owned.get("baseline_lineage", [plan.action_baseline_sha256]),
            original_files=frozen_files(service.root),
        )
        manifest_gate(m, config)
        archive = service.root / "successor-origins" / (sha(raw) + ".json")
        archive.parent.mkdir(exist_ok=True)
        if archive.exists():
            require(regular(archive) == raw, "PAPER_SUCCESSOR_ORIGIN_CHANGED")
        else:
            atomic(archive, raw)
        if stage:
            stage("origin_saved")
        for key in RETIRED:
            state.pop(key, None)
        state["calendar_successor"] = m.model_dump(mode="json")
        service._save(
            state,
            "holding_successor_bound",
            str(plan.day),
            parent=m.parent_checkpoint,
            manifest=digest(m),
        )
        if stage:
            stage("binding_saved")
        verify_state(service.root, state, config, current)
        return state
