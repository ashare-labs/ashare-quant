"""Explicit one-day runtime extension of a settled v2 native account.

The original account identity, config, engine pin and checkpoint bytes remain
unchanged. A separately frozen runtime may append one holding day only, after
an exact closed-state binding. It cannot finish or modify the original day.
"""

import json
from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.contracts import Contract
from ashare_quant.m2_jobs.files import atomic, digest, lock, regular, sha

from .contracts import Sha
from .intraday_contracts import IntradayPlan
from .risk import require


def now():
    return datetime.now(timezone.utc)


class Continuation(Contract):
    schema_version: str = "paper-intraday-hold-continuation.v1"
    origin_identity: Sha
    origin_engine_pin: dict
    runtime_pin: dict
    origin_state_sha256: Sha
    parent_checkpoint: Sha
    parent_report_sha256: Sha
    bound_at: AwareDatetime
    origin_plan: IntradayPlan
    plan: IntradayPlan
    hold_targets: dict[str, int] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def finite(self):
        a, b = self.origin_plan, self.plan
        mutable = {"day", "start_at", "intent_deadline", "execution_deadline", "daily_capture"}
        require(
            self.schema_version == "paper-intraday-hold-continuation.v1"
            and set(self.origin_engine_pin) == set(self.runtime_pin) == {"sources", "runtime", "rq"}
            and self.origin_engine_pin["runtime"] == self.runtime_pin["runtime"]
            and self.origin_engine_pin["rq"] == self.runtime_pin["rq"]
            and a.model == b.model == "prior-close-intent-minute-partial-cancel.v2"
            and b.day == a.day + timedelta(days=1)
            and (a.day.year, a.day.month) == (b.day.year, b.day.month)
            and self.bound_at < b.start_at
            and {k: v for k, v in a.model_dump().items() if k not in mutable}
            == {k: v for k, v in b.model_dump().items() if k not in mutable}
            and set(self.hold_targets) == set(a.universe)
            and all(type(q) is int and q >= 0 for q in self.hold_targets.values()),
            "PAPER_CONTINUATION_SCOPE",
        )
        return self


def manifest_gate(value, config, decision=None):
    m = Continuation.model_validate(value)
    require(
        digest(m.origin_plan) == config.scenario_manifest_sha256
        and m.plan.model == config.model
        and m.plan.universe == config.universe
        and m.plan.initial_cash == config.initial_cash
        and m.plan.admission_reader_sha256 == config.admission_reader_sha256,
        "PAPER_CONTINUATION_IDENTITY",
    )
    if decision is not None:
        receipt = decision.strategy_receipt or {}
        require(
            decision.trading_date == m.plan.day
            and decision.previous_open == m.origin_plan.day
            and decision.expected_parent == m.parent_checkpoint
            and decision.targets == m.hold_targets
            and receipt.get("scheduled") is False
            and receipt.get("signal_capture") == m.plan.daily_capture
            and receipt.get("equity_from") == "previous_native_checkpoint"
            and receipt.get("intraday_plan") == m.plan.model_dump(mode="json"),
            "PAPER_CONTINUATION_HOLD_ONLY",
        )
    return m


def verify_state(root, state, config, current_pin):
    m = manifest_gate(state["continuation"], config)
    origin_raw = regular(root / "continuation-origin.json")
    require(sha(origin_raw) == m.origin_state_sha256, "PAPER_CONTINUATION_ORIGIN_CHANGED")
    origin = json.loads(origin_raw)
    day = str(m.origin_plan.day)
    report_raw = regular(root / day / "closing-report.json")
    require(sha(report_raw) == m.parent_report_sha256, "PAPER_REPORT_CHANGED")
    report = json.loads(report_raw)
    require(
        state["identity"] == origin["identity"] == m.origin_identity
        and state["config"] == origin["config"]
        and state["engine_pin"] == origin["engine_pin"] == m.origin_engine_pin
        and current_pin == m.runtime_pin
        and origin["head"] == m.parent_checkpoint
        and origin["last_day"] == day
        and set(origin["days"]) == {day}
        and state["days"][day] == origin["days"][day]
        and set(state["days"]) <= {day, str(m.plan.day)}
        and state["events"][: len(origin["events"])] == origin["events"]
        and origin["days"][day]["reports"]["closing"]["report_sha256"] == m.parent_report_sha256
        and origin["days"][day]["status"] == "committed"
        and origin["days"][day]["decision"]["next_open"] == str(m.plan.day)
        and origin["days"][day]["close"]["data_manifest_sha256"] == m.plan.daily_capture
        and m.hold_targets
        == {s: report["native"]["account"]["positions"][s]["quantity"] for s in config.universe}
        and digest(origin["days"][day]["decision"]["strategy_receipt"]["intraday_plan"])
        == digest(m.origin_plan),
        "PAPER_CONTINUATION_BINDING_CHANGED",
    )
    return m


def verify_request(request, config, decision, current_pin):
    m = manifest_gate(request["continuation"], config, decision)
    require(
        m.runtime_pin == current_pin
        and m.origin_engine_pin == request["engine_pin"]
        and m.origin_identity == request["identity"]
        and request["parent_sha"] == m.parent_checkpoint
        and request["parent_complete"] is True
        and request["base_cursor"] == 4
        and decision.strategy_receipt.get("continuation") == m.model_dump(mode="json"),
        "PAPER_CONTINUATION_RUNTIME_CHANGED",
    )


def bind(
    service, reader, plan, expected_state_sha, expected_runtime_sha, expected_origin_runtime_sha
):
    """Bind a reviewed successor only after the original runtime settled today."""
    from .service import engine_pin

    plan = IntradayPlan.model_validate(plan)
    current = engine_pin()
    require(digest(current) == expected_runtime_sha, "PAPER_FROZEN_RUNTIME_CHANGED")
    with lock(service.root / "writer.lock"):
        raw = regular(service.path)
        state, config = service._load(verify=False)
        require(
            digest(state["engine_pin"]) == expected_origin_runtime_sha,
            "PAPER_CONTINUATION_ORIGIN_RUNTIME",
        )
        if "continuation" in state:
            m = verify_state(service.root, state, config, current)
            require(
                m.origin_state_sha256 == expected_state_sha and m.plan == plan,
                "PAPER_CONTINUATION_IDEMPOTENCY_CONFLICT",
            )
            return state
        require(sha(raw) == expected_state_sha, "PAPER_CONTINUATION_STATE_CHANGED")
        day = state["last_day"]
        require(
            day is not None
            and set(state["days"]) == {day}
            and state["days"][day]["status"] == "committed",
            "PAPER_CONTINUATION_CLOSE_REQUIRED",
        )
        previous = service._report(state, day, "closing")
        row = state["days"][day]
        origin_plan = IntradayPlan.model_validate(
            row["decision"]["strategy_receipt"]["intraday_plan"]
        )
        owned = reader.reviewed_plan()
        packet = reader.read(config, "daily", plan.daily_capture)
        require(
            str(plan.day) == row["decision"]["next_open"] == packet["calendar"]["next_open"]
            and packet["signal_day"] == day
            and packet["signal_ready"]
            and not packet["revisions"]
            and row["close"]["data_manifest_sha256"] == plan.daily_capture
            and owned["first_window"][0] <= day < str(plan.day) <= owned["first_window"][1]
            and owned["action_baseline_sha256"] == plan.action_baseline_sha256
            and owned["admission_reader_sha256"] == plan.admission_reader_sha256,
            "PAPER_CONTINUATION_OWNER_SCOPE",
        )
        account = previous["native"]["account"]
        require(
            account["open_orders"] == [] and previous["native"]["settlement_count"] == 1,
            "PAPER_CONTINUATION_NATIVE_PARENT",
        )
        at = now()
        require(
            datetime.fromisoformat(row["close"]["received_at"]) <= at,
            "PAPER_CONTINUATION_CLOSE_NOT_VISIBLE",
        )
        m = Continuation(
            origin_identity=state["identity"],
            origin_engine_pin=state["engine_pin"],
            runtime_pin=current,
            origin_state_sha256=sha(raw),
            parent_checkpoint=state["head"],
            parent_report_sha256=row["reports"]["closing"]["report_sha256"],
            bound_at=at,
            origin_plan=origin_plan,
            plan=plan,
            hold_targets={s: account["positions"][s]["quantity"] for s in config.universe},
        )
        manifest_gate(m, config)
        backup = service.root / "continuation-origin.json"
        if backup.exists():
            require(regular(backup) == raw, "PAPER_CONTINUATION_ORIGIN_CHANGED")
        else:
            atomic(backup, raw)
        state["continuation"] = m.model_dump(mode="json")
        service._save(
            state,
            "holding_successor_bound",
            str(plan.day),
            parent=state["head"],
            manifest=digest(m),
        )
        return state
