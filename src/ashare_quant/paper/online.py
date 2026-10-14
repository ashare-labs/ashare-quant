"""Local, single-writer, staged forward paper account using the native ledger.

Only the owner adapter may supply observed inputs. Fixture APIs are confined to
calendar_fixture identities. Control metadata contains no independently posted
cash or positions. No process stays alive between invocations.
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, time, timezone
from decimal import Decimal
from importlib import metadata
from pathlib import Path

from ashare_quant.contracts import SHANGHAI
from ashare_quant.m2_jobs.files import atomic, canonical, digest, lock, regular, sha

from .online_contracts import (
    CONDITIONAL_MODEL,
    INTRADAY_MODELS,
    CalendarReplan,
    OnlineClose,
    OnlineDecision,
    OnlineSession,
    OpeningInput,
)
from .online_gates import closing_gate, opening_gate
from .risk import decision_gate, require
from .runtime_migration import authorization_event, selected_continuation
from .service import completed_proof, engine_pin, read_checkpoint


def now():
    return datetime.now(timezone.utc)


def published_open_consistency(payload, opened, opening_payload, row, base):
    """Closing is a continuation of the published open, never a replay of it."""
    state = json.loads(payload["states"]["mod_paper"])
    cursor = json.loads(payload["states"]["event_source"])["cursor"]
    require(
        state["day"] == row["day"]
        and state["input_id"] == row["input_id"]
        and base + 2 <= cursor <= base + 4,
        "PAPER_PUBLISHED_OPEN_ROLLBACK",
    )
    for key in (
        "trades",
        "before",
        "opening_before",
        "opening_reconciliation",
        "risk",
        "submitted",
        "submissions",
        "refusals",
    ):
        require(state[key] == opened[key], "PAPER_PUBLISHED_OPEN_CHANGED", key)
    require(
        state["events"][:2] == opened["events"]
        and payload["extras"]["ids"] == opening_payload["extras"]["ids"],
        "PAPER_PUBLISHED_OPEN_CHANGED",
    )
    require(set(state["orders"]) == set(opened["orders"]), "PAPER_PUBLISHED_OPEN_CHANGED")
    for oid, order in state["orders"].items():
        for key in ("id", "security", "side", "quantity", "filled", "cost", "average_price"):
            require(order[key] == opened["orders"][oid][key], "PAPER_PUBLISHED_OPEN_CHANGED")
    if payload["receipt"]:
        require(
            payload["receipt"]["parent_checkpoint"] == row["decision"]["expected_parent"],
            "PAPER_PARENT_CONFLICT",
        )


class OnlineService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.json"

    def _save(self, state, kind, day=None, **details):
        state["events"].append(
            {"at": now().isoformat(), "kind": kind, "day": day, "details": details}
        )
        atomic(self.path, state)

    def _load(self, *, verify=True):
        state = json.loads(regular(self.path))
        config = OnlineSession.model_validate(state["config"])
        require(
            state["identity"]
            == digest({"config": config.model_dump(mode="json"), "engine": state["engine_pin"]}),
            "PAPER_SESSION_CHANGED",
        )
        if verify and selected_continuation(state) is not None:
            from .intraday_continuation import verify_state

            verify_state(self.root, state, config, engine_pin())
        elif verify:
            require(state["engine_pin"] == engine_pin(), "PAPER_RUNTIME_CHANGED")
        if "calendar_plan" in state:
            plan = CalendarReplan.model_validate(state["calendar_plan"])
            require(digest(plan) == config.scenario_manifest_sha256, "PAPER_CALENDAR_PLAN_CHANGED")
            require(
                plan.admission_reader_sha256 == config.admission_reader_sha256
                and plan.scope == config.universe
                and plan.model == config.model,
                "PAPER_CALENDAR_PLAN_CHANGED",
            )
        return state, config

    def create(self, config):
        config = OnlineSession.model_validate(config)
        pin = engine_pin()
        identity = digest({"config": config.model_dump(mode="json"), "engine": pin})
        with lock(self.root / "writer.lock"):
            if self.path.exists():
                state, _ = self._load()
                require(state["identity"] == identity, "PAPER_SESSION_IDEMPOTENCY_CONFLICT")
                return state
            state = dict(
                schema="paper-calendar-control.v1",
                config=config.model_dump(mode="json"),
                engine_pin=pin,
                identity=identity,
                head=None,
                last_day=None,
                days={},
                events=[],
            )
            self._save(state, "session_created", data_mode=config.data_mode)
            return state

    def migrate_empty(self, owner_pin, expected_state_sha256, reason, *, plan=None):
        """Explicit new code identity only before any intent or native account."""
        require(bool(reason.strip()), "PAPER_RECOVERY_REASON")
        with lock(self.root / "writer.lock"):
            raw = regular(self.path)
            state, old_config = self._load(verify=False)
            require(sha(raw) == expected_state_sha256, "PAPER_MIGRATION_STATE_CHANGED")
            require(
                state["days"] == {}
                and state["head"] is None
                and state["last_day"] is None
                and not list(self.root.rglob("*.chk"))
                and not list(self.root.glob("????-??-??"))
                and not list(self.root.rglob("request.json")),
                "PAPER_MIGRATION_REQUIRES_EMPTY_ACCOUNT",
            )
            changes = {"admission_reader_sha256": owner_pin}
            if plan is not None:
                plan = CalendarReplan.model_validate(plan)
                require(
                    plan.previous_scenario_manifest_sha256 == old_config.scenario_manifest_sha256
                    and plan.admission_reader_sha256 == owner_pin
                    and plan.scope == old_config.universe
                    and (
                        plan.model == old_config.model
                        or (
                            old_config.model == "prior-close-standing-intent-observed-open.v1"
                            and plan.model == CONDITIONAL_MODEL
                        )
                    ),
                    "PAPER_REPLAN_IDENTITY",
                )
                changes["scenario_manifest_sha256"] = digest(plan)
                changes["model"] = plan.model
            elif "calendar_plan" in state:
                require(owner_pin == old_config.admission_reader_sha256, "PAPER_REPLAN_REQUIRED")
            config = OnlineSession.model_validate({**old_config.model_dump(mode="json"), **changes})
            backup = self.root / "migrations" / (expected_state_sha256 + ".json")
            backup.parent.mkdir(exist_ok=True, mode=0o700)
            if backup.exists():
                require(regular(backup) == raw, "PAPER_MIGRATION_BACKUP_CHANGED")
            else:
                atomic(backup, raw)
            if plan is not None:
                plan_path = self.root / "migrations" / ("plan-" + digest(plan) + ".json")
                content = canonical(plan)
                if plan_path.exists():
                    require(regular(plan_path) == content, "PAPER_MIGRATION_BACKUP_CHANGED")
                else:
                    atomic(plan_path, content)
                state["calendar_plan"] = plan.model_dump(mode="json")
            prior_identity = state["identity"]
            state["config"] = config.model_dump(mode="json")
            state["engine_pin"] = engine_pin()
            state["identity"] = digest({"config": state["config"], "engine": state["engine_pin"]})
            self._save(
                state,
                "explicit_empty_identity_migration",
                reason=reason,
                previous_identity=prior_identity,
                previous_state_sha256=expected_state_sha256,
                new_identity=state["identity"],
                archive=str(backup),
                previous_scenario_manifest_sha256=old_config.scenario_manifest_sha256,
                new_scenario_manifest_sha256=config.scenario_manifest_sha256,
            )
            return state

    @staticmethod
    def _window(config, day, start, end, received, decision=None):
        if config.data_mode == "calendar_observed":
            at = now().astimezone(SHANGHAI)
            if config.model in INTRADAY_MODELS and start.hour == 9:
                from .intraday_contracts import continuous, plan_for

                plan = plan_for(config, decision)
                cutoff = plan.intent_deadline if start.minute == 0 else plan.execution_deadline
                require(
                    at.date().isoformat() == day
                    and plan.start_at <= received <= at <= cutoff
                    and continuous(at),
                    "PAPER_INTRADAY_REAL_WINDOW_CLOSED",
                )
                return
            require(
                at.date().isoformat() == day and start <= at.time() <= end and received <= at,
                "PAPER_REAL_WINDOW_CLOSED",
            )

    @staticmethod
    def _closing_window(config, received):
        # Closing only values/settles an existing open checkpoint. Its owner-verified
        # same-day receipt may be processed later; it must never come from the future.
        if config.data_mode == "calendar_observed":
            require(received <= now(), "PAPER_CLOSE_NOT_YET_RECEIVED")

    def prepare_fixture(self, value):
        _, config = self._load()
        require(config.data_mode == "calendar_fixture", "PAPER_OWNER_ADAPTER_REQUIRED")
        return self._prepare(OnlineDecision.model_validate(value))

    def prepare_observed(self, reader, capture, admission):
        from .online_adapter import decision_from_owner

        state, config = self._load()
        require(config.data_mode == "calendar_observed", "PAPER_EVIDENCE_MODE_MISMATCH")
        # A duplicate delivery reuses the original wall-clock receipt; it must
        # still be read by its owner, rather than manufacture a second timestamp.
        packet = reader.read(config, "daily", capture)
        old = state["days"].get(packet["calendar"]["next_open"])
        if old:
            reader.read(config, "admission", admission)
            receipt = old["decision"]["strategy_receipt"]
            require(
                receipt["signal_capture"] == capture and receipt["admission_capture"] == admission,
                "PAPER_IDEMPOTENCY_CONFLICT",
            )
            return old
        previous = self.report(state["last_day"], "closing") if state["last_day"] else None
        decision = decision_from_owner(config, state, previous, reader, capture, admission)
        return self._prepare(decision)

    def _prepare(self, decision):
        day = decision.trading_date.isoformat()
        with lock(self.root / "writer.lock"):
            state, config = self._load()
            if selected_continuation(state) is not None:
                require(
                    (decision.strategy_receipt or {}).get("continuation")
                    == selected_continuation(state),
                    "PAPER_CONTINUATION_MANIFEST_CHANGED",
                )
            if "calendar_plan" in state:
                plan = CalendarReplan.model_validate(state["calendar_plan"])
                days = tuple(d.isoformat() for d in plan.first_window)
                require(day in days and (bool(state["days"]) or day == days[0]), "PAPER_REPLAN_DAY")
                if config.data_mode == "calendar_observed":
                    evidence = (decision.strategy_receipt or {}).get("action_adjudications", {})
                    require(
                        set(evidence) == set(config.universe)
                        and all(
                            evidence[s]["action_scope"]["price_share_code_review"][
                                "baseline_sha256"
                            ]
                            == plan.action_baseline_sha256
                            for s in config.universe
                        ),
                        "PAPER_REPLAN_ACTION_BASELINE",
                    )
            decision_gate(config, decision)
            old = state["days"].get(day)
            if old:
                require(
                    old["decision"] == decision.model_dump(mode="json"),
                    "PAPER_IDEMPOTENCY_CONFLICT",
                )
                return old
            self._window(config, day, time(9), time(9, 5), decision.received_at, decision)
            require(state["head"] == decision.expected_parent, "PAPER_PARENT_CONFLICT")
            require(
                all(r["status"] == "committed" for r in state["days"].values()),
                "PAPER_PREVIOUS_DAY_PENDING",
            )
            if state["last_day"]:
                require(state["last_day"] == str(decision.previous_open), "PAPER_DAY_GAP")
                prior = state["days"][state["last_day"]]
                require(prior["decision"]["next_open"] == day, "PAPER_DAY_GAP")
                report = self._report(state, state["last_day"], "closing")
                for row in decision.rows:
                    require(
                        Decimal(row.previous_close)
                        == Decimal(report["native"]["account"]["positions"][row.security]["price"]),
                        "PAPER_PRIOR_MARK_REVISION",
                    )
            intent = digest(
                {"slot": [config.account_id, state["identity"], day], "decision": digest(decision)}
            )
            row = dict(
                day=day,
                decision=decision.model_dump(mode="json"),
                intent_id=intent,
                opening=None,
                close=None,
                input_id=None,
                status="prepared",
                phase=None,
                attempts=0,
                error=None,
                reports={},
            )
            (self.root / day).mkdir(exist_ok=True, mode=0o700)
            state["days"][day] = row
            self._save(state, "intent_committed", day, intent_id=intent)
            return row

    def deliver_fixture(self, phase, value):
        _, config = self._load()
        require(config.data_mode == "calendar_fixture", "PAPER_OWNER_ADAPTER_REQUIRED")
        return self._deliver(phase, value)

    def deliver_observed(self, reader, day, phase, capture):
        from .online_adapter import execution_from_owner

        state, config = self._load()
        require(config.data_mode == "calendar_observed", "PAPER_EVIDENCE_MODE_MISMATCH")
        value = execution_from_owner(config, state["days"][day], reader, phase, capture)
        return self._deliver(phase, value)

    def _deliver(self, phase, value):
        require(phase in {"opening", "closing"}, "PAPER_PHASE")
        packet = (OpeningInput if phase == "opening" else OnlineClose).model_validate(value)
        day = str(packet.trading_date)
        field = "opening" if phase == "opening" else "close"
        with lock(self.root / "writer.lock"):
            state, config = self._load()
            require(day in state["days"], "PAPER_DAY_NOT_FOUND")
            row = state["days"][day]
            require(row["intent_id"] == packet.intent_id, "PAPER_INTENT_MISMATCH")
            if row[field]:
                require(row[field] == packet.model_dump(mode="json"), "PAPER_IDEMPOTENCY_CONFLICT")
                return row
            require(row["status"] not in {"running", "failed"}, "PAPER_RESUME_REQUIRED")
            decision = OnlineDecision.model_validate(row["decision"])
            if phase == "opening":
                opening_gate(config, decision, packet)
                self._window(config, day, time(9, 30), time(9, 35), packet.received_at, decision)
                row["input_id"] = digest(
                    {
                        "decision": digest(decision),
                        "opening": digest(packet),
                        "intent": packet.intent_id,
                    }
                )
            else:
                require("opening" in row["reports"], "PAPER_OPENING_PENDING")
                self._report(state, day, "opening")
                closing_gate(config, decision, OpeningInput.model_validate(row["opening"]), packet)
                self._closing_window(config, packet.received_at)
            row[field], row["phase"], row["status"] = packet.model_dump(mode="json"), phase, "ready"
            self._save(state, "input_sealed", day, phase=phase, input_sha256=digest(packet))
            return row

    def _report(self, state, day, phase):
        row = state["days"][day]
        require(phase in row["reports"], "PAPER_REPORT_NOT_COMMITTED")
        pointer = row["reports"][phase]
        root = self.root / day
        payload, _ = read_checkpoint(
            root / (phase + ".chk"), state["identity"], pointer["checkpoint_sha256"]
        )
        raw = regular(root / (phase + "-report.json"))
        require(sha(raw) == pointer["report_sha256"], "PAPER_REPORT_CHANGED")
        report = json.loads(raw)
        base = 0
        if row["decision"]["expected_parent"]:
            prior = state["days"][row["decision"]["previous_open"]]
            require(
                prior["reports"]["closing"]["checkpoint_sha256"]
                == row["decision"]["expected_parent"],
                "PAPER_PARENT_CONFLICT",
            )
            base = self._report(state, prior["day"], "closing")["native"]["cursor"]
        native = completed_proof(
            payload,
            {**row, "decision": canonical(row["decision"]).decode()},
            row["decision"]["expected_parent"],
            base,
            complete=phase == "closing",
        )
        require(
            report["native"] == native
            and report["checkpoint_sha256"] == pointer["checkpoint_sha256"],
            "PAPER_REPORT_CHANGED",
        )
        require(
            native["phase"] == phase and native["opening_sha256"] == digest(row["opening"]),
            "PAPER_PHASE_PROOF",
        )
        m = selected_continuation(state)
        if m is not None and day == m["plan"]["day"]:
            expected = dict(
                manifest_sha256=digest(m),
                engine_pin_sha256=digest(m["runtime_pin"]),
                original_parent=m["parent_checkpoint"],
            )
            if "runtime_authorization" in state:
                a = state["runtime_authorization"]
                expected.update(
                    engine_pin_sha256=digest(a["to_runtime_pin"]), authorization_sha256=digest(a)
                )
            require(
                native.get("continuation_runtime") == expected,
                "PAPER_CONTINUATION_RECEIPT_CHANGED",
            )
        if phase == "closing":
            require(native["close_sha256"] == digest(row["close"]), "PAPER_PHASE_PROOF")
            opened = self._report(state, day, "opening")
            opening_payload, _ = read_checkpoint(
                root / "opening.chk", state["identity"], opened["checkpoint_sha256"]
            )
            published_open_consistency(payload, opened["native"], opening_payload, row, base)
        return report

    def report(self, day, phase):
        state, _ = self._load()
        return self._report(state, day, phase)

    def resume(self, reason):
        require(bool(reason.strip()), "PAPER_RECOVERY_REASON")
        with lock(self.root / "writer.lock"):
            state, _ = self._load()
            for day, row in state["days"].items():
                if row["status"] not in {"failed", "running"}:
                    continue
                path = self.root / day / "native.chk"
                if path.exists():
                    read_checkpoint(path, state["identity"])
                row["status"], row["error"] = "ready", None
            self._save(state, "explicit_resume", reason=reason)
            return state

    def advance(self, day, phase, *, fault=None, stage=None):
        _, config = self._load()
        global_lock = (
            Path(tempfile.gettempdir())
            / ("ashare-paper-locks-" + str(os.getuid()))
            / (digest(config.account_id) + ".lock")
        )
        with lock(global_lock) as serial, lock(self.root / "writer.lock") as writer:
            state, config = self._load()
            row = state["days"][day]
            if phase in row["reports"]:
                return self._report(state, day, phase)
            require(phase in {"opening", "closing"} and row["phase"] == phase, "PAPER_PHASE")
            require(row["status"] == "ready", "PAPER_RESUME_REQUIRED")
            root = self.root / day
            parent = row["decision"]["expected_parent"]
            base = 0
            if parent:
                previous_day = row["decision"]["previous_open"]
                previous = self._report(state, previous_day, "closing")
                require(previous["checkpoint_sha256"] == parent, "PAPER_PARENT_CONFLICT")
                base = previous["native"]["cursor"]
                if phase == "opening" and not (root / "native.chk").exists():
                    atomic(root / "native.chk", regular(self.root / previous_day / "closing.chk"))
            request = dict(
                online=True,
                phase=phase,
                session=state["config"],
                decision=row["decision"],
                opening=row["opening"],
                close=row["close"] if phase == "closing" else None,
                identity=state["identity"],
                engine_pin=state["engine_pin"],
                input_id=row["input_id"],
                base_cursor=base,
                parent_complete=bool(parent),
                parent_sha=parent,
            )
            m = selected_continuation(state)
            if m is not None:
                require(day == m["plan"]["day"], "PAPER_CONTINUATION_PARENT_FROZEN")
                request["continuation"] = m
                request["continuation_binding"] = next(
                    e for e in reversed(state["events"]) if e["kind"] == "holding_successor_bound"
                )
                if "runtime_authorization" in state:
                    request["runtime_authorization"] = state["runtime_authorization"]
                    request["runtime_authorization_event"] = authorization_event(state)
            request_path = root / (phase + "-request.json")
            if request_path.exists():
                require(regular(request_path) == canonical(request), "PAPER_REQUEST_CHANGED")
            else:
                atomic(request_path, request)
            row.update(status="running", error=None, attempts=row["attempts"] + 1)
            self._save(state, "worker_started", day, phase=phase)
            try:
                done = False
                opened = opening_payload = None
                if phase == "closing":
                    opened = self._report(state, day, "opening")
                    opening_payload, _ = read_checkpoint(
                        root / "opening.chk", state["identity"], opened["checkpoint_sha256"]
                    )
                    if not (root / "native.chk").exists():
                        atomic(root / "native.chk", regular(root / "opening.chk"))
                    working, _ = read_checkpoint(root / "native.chk", state["identity"])
                    published_open_consistency(
                        working, opened["native"], opening_payload, row, base
                    )
                if (root / "native.chk").exists():
                    payload, _ = read_checkpoint(root / "native.chk", state["identity"])
                    receipt = payload["receipt"]
                    done = bool(
                        receipt
                        and receipt.get("input_id") == row["input_id"]
                        and receipt.get("phase") == phase
                    )
                    if done:
                        completed_proof(
                            payload,
                            {**row, "decision": canonical(row["decision"]).decode()},
                            parent,
                            base,
                            complete=phase == "closing",
                        )
                if not done:
                    if phase == "opening":
                        self._window(
                            config,
                            day,
                            time(9, 30),
                            time(9, 35),
                            OpeningInput.model_validate(row["opening"]).received_at,
                            OnlineDecision.model_validate(row["decision"]),
                        )
                    else:
                        self._closing_window(
                            config,
                            OnlineClose.model_validate(row["close"]).received_at,
                        )
                    self._spawn(request_path, serial, writer, fault, stage)
                payload, checkpoint_sha = read_checkpoint(root / "native.chk", state["identity"])
                if phase == "closing":
                    published_open_consistency(
                        payload, opened["native"], opening_payload, row, base
                    )
                native = completed_proof(
                    payload,
                    {**row, "decision": canonical(row["decision"]).decode()},
                    parent,
                    base,
                    complete=phase == "closing",
                )
                require(native["phase"] == phase, "PAPER_PHASE_PROOF")
                report = dict(
                    day=day,
                    phase=phase,
                    native=native,
                    checkpoint_sha256=checkpoint_sha,
                    real_calendar_observation=config.data_mode == "calendar_observed",
                    continuous_paper_accepted=False,
                )
                if stage:
                    stage("before_publish", None)
                atomic(root / (phase + ".chk"), regular(root / "native.chk"))
                atomic(root / (phase + "-report.json"), report)
                row["reports"][phase] = dict(
                    checkpoint_sha256=checkpoint_sha, report_sha256=digest(report)
                )
                row["status"] = "committed" if phase == "closing" else "awaiting_close"
                if phase == "closing":
                    state["head"], state["last_day"] = checkpoint_sha, day
                self._save(
                    state,
                    "phase_committed",
                    day,
                    phase=phase,
                    checkpoint_sha256=checkpoint_sha,
                    refusals=native["refusals"],
                    target_gaps=native["target_gaps"],
                )
                return self._report(state, day, phase)
            except BaseException as exc:
                row.update(
                    status="failed",
                    error={
                        "code": getattr(exc, "code", "PAPER_NATIVE_FAILED"),
                        "message": str(exc),
                    },
                )
                self._save(state, "worker_failed", day, phase=phase, error=row["error"])
                raise

    @staticmethod
    def _spawn(request, serial, writer, fault, stage):
        paths = [
            str(Path(__file__).resolve().parents[2]),
            str(metadata.distribution("ashare-data").locate_file("")),
            str(metadata.distribution("rqalpha").locate_file("")),
        ]
        boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['paper-native',a];from ashare_quant.paper.native import main;main()"
        env = {
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "Asia/Shanghai",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "PAPER_PARENT_PID": str(os.getpid()),
        }
        if fault:
            env["PROBE_KILL" if fault.startswith("checkpoint:") else "PAPER_FAULT"] = (
                fault.removeprefix("checkpoint:")
            )
        with (request.parent / ("native-" + uuid.uuid4().hex + ".log")).open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, str(request)])],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(serial.fileno(), writer.fileno()),
            )
            try:
                if stage:
                    stage("spawned", process)
                process.wait(timeout=120)
                require(
                    process.returncode == 0,
                    "PAPER_NATIVE_FAILED",
                    f"Native worker exited {process.returncode}; see {log.name}",
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def status(self):
        state, config = self._load(verify=False)
        result = dict(
            state,
            continuous_paper_accepted=False,
            real_calendar_observation=config.data_mode == "calendar_observed",
        )
        result["runtime_matches"] = state["engine_pin"] == engine_pin()
        if selected_continuation(state) is not None:
            result["origin_runtime_matches"] = result["runtime_matches"]
            try:
                self._load()
                result["runtime_matches"] = True
                result["continuation_runtime_validated"] = True
            except Exception as exc:
                result["runtime_matches"] = False
                result["continuation_runtime_validated"] = False
                result["continuation_error"] = str(exc)
        result["latest_account"] = None
        for day, row in state["days"].items():
            row["recovery_required"] = row["status"] in {"failed", "running"}
            for phase in ("opening", "closing"):
                if phase in row["reports"]:
                    try:
                        result["latest_account"] = self._report(state, day, phase)["native"][
                            "account"
                        ]
                    except Exception as exc:
                        row["status"], row["recovery_required"] = "evidence_conflict", True
                        row["error"] = {
                            "code": getattr(exc, "code", "PAPER_EVIDENCE_CONFLICT"),
                            "message": str(exc),
                        }
                        result["latest_account"] = None
        return result
