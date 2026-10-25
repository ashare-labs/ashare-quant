"""Local control state and native checkpoint publication. No account balances in SQL."""

import json
import os
import pickle
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import jsonpickle

from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_sha256
from ashare_quant.paper.contracts import CloseInput, Decision, SessionConfig
from ashare_quant.paper.native_worker import spawn
from ashare_quant.paper.risk import close_gate, decision_gate, reconcile, reconcile_opening, require
from ashare_quant.runtime import runtime_fingerprint
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, sha

LEASE_SECONDS = 15


def engine_pin():
    from ashare_quant.execution.rqalpha.runtime import runtime_identity

    return {
        "sources": source_sha256(),
        "runtime": runtime_fingerprint(native=True),
        "rq": digest(runtime_identity()),
    }


def read_checkpoint(path, identity, expected_sha=None):
    raw = regular(path)
    if expected_sha is not None:
        require(sha(raw) == expected_sha, "PAPER_CHECKPOINT_CHANGED")
    try:
        header, payload = raw.split(b"\n", 1)
        require(
            json.loads(header) == {"schema": 1, "identity": identity, "sha256": sha(payload)},
            "PAPER_CHECKPOINT_IDENTITY",
        )
        # Trusted locally produced native serialization only; never accept this over HTTP.
        value = pickle.loads(payload)
        require(
            set(value) == {"states", "extras", "complete", "receipt"}, "PAPER_CHECKPOINT_SCHEMA"
        )
        return value, sha(raw)
    except (ValueError, KeyError, TypeError, pickle.UnpicklingError, EOFError) as exc:
        raise DomainError(
            "PAPER_CHECKPOINT_CORRUPT", "Native checkpoint is not readable", 409
        ) from exc


def completed_proof(payload, row, parent, base_cursor, *, complete=True):
    from decimal import Decimal

    from jsonpickle.ext import numpy as jsonpickle_numpy
    from rqalpha.const import POSITION_DIRECTION

    receipt = payload["receipt"]
    require(
        payload["complete"] is complete
        and receipt["input_id"] == row["input_id"]
        and receipt["day"] == row["day"]
        and receipt["parent_checkpoint"] == parent
        and receipt["cursor"] == base_cursor + (4 if complete else 2),
        "PAPER_COMPLETION_PROOF",
    )
    state = json.loads(payload["states"]["mod_paper"])
    require(all(receipt[k] == v for k, v in state.items()), "PAPER_RECEIPT_STATE_CONFLICT")
    cursor = json.loads(payload["states"]["event_source"])["cursor"]
    executor = json.loads(payload["states"]["executor"])
    require(
        cursor == receipt["cursor"]
        and executor["settled"]
        == (
            row["day"]
            if complete
            else json.loads(row["decision"])["previous_open"]
            if base_cursor
            else None
        )
        and executor["count"] == receipt["settlement_count"] == base_cursor // 4 + int(complete),
        "PAPER_NATIVE_CURSOR_OR_SETTLEMENT",
    )
    jsonpickle_numpy.register_handlers()
    portfolio = jsonpickle.decode(payload["states"]["portfolio"].decode(), keys=False)
    account = portfolio["accounts"]["STOCK"]["native"]
    reported = receipt["account"]
    decision_type = Decision
    if json.loads(row["decision"]).get("evidence_kind") in {
        "live_local_capture",
        "calendar_fixture",
        "sealed_response_replay",
    }:
        from ashare_quant.paper.online_contracts import OnlineDecision

        decision_type = OnlineDecision
    require(
        Decimal(str(account["total_cash"])) == Decimal(reported["total_cash"])
        and Decimal(str(account["frozen_cash"])) == Decimal(reported["frozen_cash"]),
        "PAPER_NATIVE_CASH_PROJECTION",
    )
    for sid, pos in reported["positions"].items():
        native = account["positions"].get(sid, {}).get(POSITION_DIRECTION.LONG)
        if native is None:
            # RQ serializes only material positions; a never-held symbol has no native entry.
            require(pos["quantity"] == pos["sellable"] == 0, "PAPER_NATIVE_POSITION_PROJECTION")
            continue
        require(
            native["quantity"] == pos["quantity"]
            and native["quantity"] - native["non_closable"] == pos["sellable"],
            "PAPER_NATIVE_POSITION_PROJECTION",
        )
        require(
            abs(
                sum(Decimal(str(amount)) for _, amount in native["dividend_receivable"])
                - Decimal(pos["dividend_receivable"])
            )
            <= Decimal("0.000001"),
            "PAPER_NATIVE_RECEIVABLE_PROJECTION",
        )
        require(
            Decimal(str(native["avg_price"])) == Decimal(pos["average_cost"]),
            "PAPER_NATIVE_COST_PROJECTION",
        )
    require(set(account["positions"]) <= set(reported["positions"]), "PAPER_NATIVE_POSITION_SCOPE")
    reconcile(receipt["before"], reported, receipt["trades"])
    require(
        receipt["opening_reconciliation"]
        == reconcile_opening(
            receipt["opening_before"],
            receipt["before"],
            decision_type.model_validate(json.loads(row["decision"])),
        ),
        "PAPER_ACTION_PROOF_CONFLICT",
    )
    return receipt


class PaperService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_sessions(
                    id TEXT PRIMARY KEY, account_id TEXT UNIQUE NOT NULL,
                    config TEXT NOT NULL, engine_pin TEXT NOT NULL, identity TEXT NOT NULL,
                    head TEXT, last_day TEXT, paused INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS paper_days(
                    session_id TEXT NOT NULL, day TEXT NOT NULL, decision TEXT NOT NULL,
                    intent_id TEXT NOT NULL UNIQUE, close_input TEXT,
                    input_id TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    token TEXT, lease_until REAL, error TEXT, checkpoint_sha TEXT, report_sha TEXT,
                    PRIMARY KEY(session_id,day));
                CREATE TABLE IF NOT EXISTS paper_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    day TEXT, at REAL NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_deliveries(
                    session_id TEXT NOT NULL, day TEXT NOT NULL, packet_sha256 TEXT NOT NULL,
                    received_at TEXT NOT NULL, PRIMARY KEY(session_id,day));
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def event(db, sid, day, kind, details):
        db.execute(
            "INSERT INTO paper_events(session_id,day,at,kind,details) VALUES(?,?,?,?,?)",
            (sid, day, time.time(), kind, canonical(details).decode()),
        )

    def alert(self, sid, day, error):
        from ashare_quant.paper.errors import error_value

        value = error_value(error)
        with self.db() as db:
            self.event(db, sid, day, "alert", value)
        return value

    def session(self, sid, *, verify=True):
        with self.db() as db:
            row = db.execute("SELECT * FROM paper_sessions WHERE id=?", (sid,)).fetchone()
        require(row is not None, "PAPER_SESSION_NOT_FOUND")
        row = dict(row)
        if verify:
            require(json.loads(row["engine_pin"]) == engine_pin(), "PAPER_RUNTIME_CHANGED")
        config = SessionConfig.model_validate_json(row["config"])
        require(
            row["identity"]
            == digest(
                {"config": config.model_dump(mode="json"), "engine": json.loads(row["engine_pin"])}
            ),
            "PAPER_SESSION_CHANGED",
        )
        return row, config

    def create(self, config):
        config = SessionConfig.model_validate(config)
        # Pydantic accepts subclass instances without revalidating Literal fields.
        # The calendar controller must not enter this historical/local API via
        # an OnlineSession Python object either.
        require(
            config.data_mode in {"synthetic_fixture", "historical_research"},
            "PAPER_SESSION_MODE_UNSUPPORTED",
        )
        pin = engine_pin()
        identity = digest({"config": config.model_dump(mode="json"), "engine": pin})
        sid = "paper-" + identity[:32]
        with self.db() as db:
            old = db.execute(
                "SELECT * FROM paper_sessions WHERE account_id=?", (config.account_id,)
            ).fetchone()
            if old:
                require(old["identity"] == identity, "PAPER_SESSION_IDEMPOTENCY_CONFLICT")
                return dict(old)
            db.execute(
                "INSERT INTO paper_sessions(id,account_id,config,engine_pin,identity) VALUES(?,?,?,?,?)",
                (
                    sid,
                    config.account_id,
                    canonical(config).decode(),
                    canonical(pin).decode(),
                    identity,
                ),
            )
            self.event(
                db,
                sid,
                None,
                "session_created",
                {"identity": identity, "data_mode": config.data_mode},
            )
        (self.root / sid).mkdir(mode=0o700)
        return self.session(sid)[0]

    def day(self, sid, day):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, str(day))
            ).fetchone()
        require(row is not None, "PAPER_DAY_NOT_FOUND")
        return dict(row)

    def prepare(self, sid, value):
        decision = Decision.model_validate(value)
        day = decision.trading_date.isoformat()
        try:
            session, config = self.session(sid)
            if config.strategy_id:
                from ashare_quant.paper.replay import build

                expected, _ = build(self, sid, day, decision.expected_parent)
                require(
                    canonical(expected) == canonical(decision), "PAPER_STRATEGY_RECEIPT_CHANGED"
                )
            decision_gate(config, decision)
            for action in decision.cash_dividends:
                record = self.report(sid, action.record_date.isoformat())
                require(
                    record["checkpoint_sha256"] == action.record_checkpoint
                    and record["native"]["account"]["positions"][action.security]["quantity"]
                    == action.record_quantity,
                    "PAPER_ACTION_RECORD_PROOF",
                )
            encoded = canonical(decision).decode()
            with self.db() as db:
                old = db.execute(
                    "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, day)
                ).fetchone()
            if old:
                require(old["decision"] == encoded, "PAPER_IDEMPOTENCY_CONFLICT")
                return dict(old)
            if session["head"]:
                from decimal import Decimal

                previous = self.report(sid, session["last_day"])["native"]["account"]
                require(
                    all(
                        Decimal(row.previous_close)
                        == Decimal(previous["positions"][row.security]["price"])
                        for row in decision.rows
                    ),
                    "PAPER_PRIOR_MARK_REVISION",
                )
            # This slot has no strategy/data/attempt version: changing those cannot create another intent.
            intent = digest(
                {"slot": [config.account_id, sid, day, "decision"], "request": digest(decision)}
            )
            with self.db() as db:
                old = db.execute(
                    "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, day)
                ).fetchone()
                if old:
                    require(old["decision"] == encoded, "PAPER_IDEMPOTENCY_CONFLICT")
                    return dict(old)
                current = db.execute("SELECT * FROM paper_sessions WHERE id=?", (sid,)).fetchone()
                require(not current["paused"], "PAPER_PAUSED")
                require(current["head"] == decision.expected_parent, "PAPER_PARENT_CONFLICT")
                pending = db.execute(
                    "SELECT day FROM paper_days WHERE session_id=? AND status!='committed'", (sid,)
                ).fetchone()
                require(pending is None, "PAPER_PREVIOUS_DAY_PENDING")
                if current["last_day"]:
                    require(
                        current["last_day"] == decision.previous_open.isoformat(), "PAPER_DAY_GAP"
                    )
                    previous = Decision.model_validate_json(
                        db.execute(
                            "SELECT decision FROM paper_days WHERE session_id=? AND day=?",
                            (sid, current["last_day"]),
                        ).fetchone()[0]
                    )
                    require(previous.next_open == decision.trading_date, "PAPER_DAY_GAP")
                else:
                    require(decision.expected_parent is None, "PAPER_GENESIS_PARENT")
                db.execute(
                    "INSERT INTO paper_days(session_id,day,decision,intent_id,status) VALUES(?,?,?,?,'prepared')",
                    (sid, day, encoded, intent),
                )
                self.event(
                    db,
                    sid,
                    day,
                    "intent_committed",
                    {"intent_id": intent, "decision_sha256": digest(decision)},
                )
            return self.day(sid, day)
        except Exception as exc:
            self.alert(sid, day, exc)
            raise

    def seal_close(self, sid, value):
        close = CloseInput.model_validate(value)
        day = close.trading_date.isoformat()
        try:
            _, config = self.session(sid)
            row = self.day(sid, day)
            decision = Decision.model_validate_json(row["decision"])
            require(close.intent_id == row["intent_id"], "PAPER_INTENT_MISMATCH")
            if config.strategy_id:
                from ashare_quant.paper.replay import build, close_for

                expected, inputs = build(self, sid, day, decision.expected_parent)
                require(
                    canonical(close_for(config, expected, inputs, row["intent_id"]))
                    == canonical(close),
                    "PAPER_REPLAY_CLOSE_CHANGED",
                )
            close_gate(config, decision, close)
            encoded = canonical(close).decode()
            with self.db() as db:
                row = db.execute(
                    "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, day)
                ).fetchone()
                if row["close_input"]:
                    require(row["close_input"] == encoded, "PAPER_IDEMPOTENCY_CONFLICT")
                else:
                    input_id = digest(
                        {
                            "decision": digest(decision),
                            "close": digest(close),
                            "intent": row["intent_id"],
                        }
                    )
                    db.execute(
                        "UPDATE paper_days SET close_input=?,input_id=?,status='ready' WHERE session_id=? AND day=?",
                        (encoded, input_id, sid, day),
                    )
                    self.event(db, sid, day, "execution_input_sealed", {"input_id": input_id})
            return self.day(sid, day)
        except Exception as exc:
            self.alert(sid, day, exc)
            raise

    def control(self, sid, action, expected_parent, reason):
        session, _ = self.session(sid)
        require(action in {"pause", "resume"}, "PAPER_CONTROL_ACTION")
        if action == "pause":
            # Finish any already running atomic native event/day; block subsequent intents.
            with self.db() as db:
                current = db.execute(
                    "SELECT head FROM paper_sessions WHERE id=?", (sid,)
                ).fetchone()[0]
                require(current == expected_parent, "PAPER_PARENT_CONFLICT")
                db.execute("UPDATE paper_sessions SET paused=1 WHERE id=?", (sid,))
                self.event(
                    db, sid, None, action, {"reason": reason, "expected_parent": expected_parent}
                )
            return self.status(sid)
        with lock(self.root / sid / "writer.lock"):
            with self.db() as db:
                row = db.execute("SELECT * FROM paper_sessions WHERE id=?", (sid,)).fetchone()
                require(row["head"] == expected_parent, "PAPER_PARENT_CONFLICT")
                if action == "resume":
                    for pending in db.execute(
                        "SELECT * FROM paper_days WHERE session_id=? AND status!='committed'",
                        (sid,),
                    ):
                        path = self.root / sid / pending["day"] / "native.chk"
                        if path.exists():
                            read_checkpoint(path, session["identity"])
                    db.execute(
                        "UPDATE paper_days SET status=CASE WHEN close_input IS NULL THEN 'prepared' ELSE 'ready' END,"
                        "error=NULL,token=NULL,lease_until=NULL WHERE session_id=? AND status!='committed'",
                        (sid,),
                    )
                db.execute(
                    "UPDATE paper_sessions SET paused=? WHERE id=?", (int(action == "pause"), sid)
                )
                self.event(
                    db, sid, None, action, {"reason": reason, "expected_parent": expected_parent}
                )
        return self.status(sid)

    def status(self, sid):
        session, _ = self.session(sid, verify=False)
        with self.db() as db:
            days = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_days WHERE session_id=? ORDER BY day", (sid,)
                )
            ]
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_events WHERE session_id=? ORDER BY sequence", (sid,)
                )
            ]
        for row in days:
            row["recovery_required"] = row["status"] in {"failed", "recovery_required"} or (
                row["status"] == "running" and (row["lease_until"] or 0) <= time.time()
            )
            row["error"] = json.loads(row["error"]) if row["error"] else None
            if row["status"] == "committed":
                directory = self.root / sid / row["day"]
                try:
                    require(
                        sha(regular(directory / "native.chk")) == row["checkpoint_sha"]
                        and sha(regular(directory / "report.json")) == row["report_sha"],
                        "PAPER_EVIDENCE_CONFLICT",
                    )
                except Exception:
                    row["status"] = "evidence_conflict"
                    row["recovery_required"] = True
                    row["error"] = {
                        "code": "PAPER_EVIDENCE_CONFLICT",
                        "message": "Committed evidence is missing or changed",
                    }
        for event in events:
            event["details"] = json.loads(event["details"])
        return {
            "session": session,
            "days": days,
            "events": events,
            "observed_data_enabled": False,
            "continuous_paper_accepted": False,
        }

    def catalogue(self):
        with self.db() as db:
            sessions = [
                dict(r)
                for r in db.execute(
                    "SELECT id,account_id,config,identity,last_day,head,paused FROM paper_sessions "
                    "ORDER BY account_id"
                )
            ]
        for row in sessions:
            row["config"] = json.loads(row["config"])
        return {"sessions": sessions, "continuous_paper_accepted": False}

    def view(self, sid, selected_day=None):
        """A verified human projection; never derives another cash/position ledger."""
        status = self.status(sid)
        session, config = self.session(sid, verify=False)
        days = status["days"]
        selected = next((d for d in days if d["day"] == selected_day), None)
        if selected_day is None and days:
            selected = days[-1]
        decision = json.loads(selected["decision"]) if selected else None
        report = (
            self.report(sid, selected["day"])
            if selected and selected["status"] == "committed"
            else None
        )
        latest = self.report(sid, session["last_day"]) if session["last_day"] else None
        replay = None
        if config.strategy_id:
            from ashare_quant.paper.replay import load, provenance

            binding, inputs = load(self, sid, config)
            pending = next((d["day"] for d in days if d["status"] != "committed"), None)
            following = next(
                (d for d in inputs.days if not session["last_day"] or d > session["last_day"]), None
            )
            replay = {
                **provenance(binding, inputs),
                "next_day": pending or following,
                "days": inputs.days,
            }
        return {
            "contract_version": "paper-api.v2",
            "session_id": sid,
            "identity": session["identity"],
            "engine_pin": json.loads(session["engine_pin"]),
            "config": config.model_dump(mode="json"),
            "head": session["head"],
            "last_day": session["last_day"],
            "paused": bool(session["paused"]),
            "days": [
                {
                    k: v
                    for k, v in d.items()
                    if k not in {"decision", "close_input", "token", "lease_until", "session_id"}
                }
                for d in days
            ],
            "selected_day": selected["day"] if selected else selected_day,
            "decision": decision,
            "report": report,
            "account_as_of": latest["day"] if latest else None,
            "latest_account": latest["native"]["account"] if latest else None,
            "events": status["events"],
            "replay": replay,
            "historical_pit_verified": False,
            "execution_permission": False,
            "observed_data_enabled": False,
            "continuous_paper_accepted": False,
        }

    def report(self, sid, day):
        row = self.day(sid, day)
        session, _ = self.session(sid, verify=False)
        root = self.root / sid / str(day)
        try:
            require(row["status"] == "committed", "PAPER_REPORT_NOT_COMMITTED")
            payload, _ = read_checkpoint(
                root / "native.chk", session["identity"], row["checkpoint_sha"]
            )
            report = regular(root / "report.json")
            require(
                sha(report) == row["report_sha"]
                and json.loads(report)["native"] == payload["receipt"],
                "PAPER_REPORT_CHANGED",
            )
            return json.loads(report)
        except Exception:
            raise

    def _heartbeat(self, sid, day, token):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, day)
            ).fetchone()
            require(row["token"] == token and row["lease_until"] > time.time(), "PAPER_FENCE_LOST")
            db.execute(
                "UPDATE paper_days SET lease_until=? WHERE session_id=? AND day=?",
                (time.time() + LEASE_SECONDS, sid, day),
            )

    def work(self, sid, day, *, fault=None, stage=None):
        session, config = self.session(sid)
        global_lock = (
            Path(tempfile.gettempdir())
            / ("ashare-paper-locks-" + str(os.getuid()))
            / (digest(config.account_id) + ".lock")
        )
        with lock(global_lock) as serial, lock(self.root / sid / "writer.lock") as writer:
            row = self.day(sid, day)
            if row["status"] == "committed":
                return self.report(sid, day)
            token = uuid.uuid4().hex
            with self.db() as db:
                current = db.execute("SELECT * FROM paper_sessions WHERE id=?", (sid,)).fetchone()
                require(not current["paused"], "PAPER_PAUSED")
                require(row["close_input"] is not None, "PAPER_CLOSE_PENDING")
                require(
                    row["status"] == "ready"
                    or row["status"] == "running"
                    and row["lease_until"] <= time.time(),
                    "PAPER_RESUME_REQUIRED",
                )
                db.execute(
                    "UPDATE paper_days SET status='running',token=?,lease_until=?,attempts=attempts+1,error=NULL "
                    "WHERE session_id=? AND day=?",
                    (token, time.time() + LEASE_SECONDS, sid, day),
                )
                self.event(db, sid, day, "worker_claimed", {"fence": digest(token)})
            process = None
            try:
                root = self.root / sid / day
                root.mkdir(mode=0o700, exist_ok=True)
                decision = Decision.model_validate_json(row["decision"])
                base_cursor, parent_complete = 0, False
                if decision.expected_parent:
                    previous = self.root / sid / decision.previous_open.isoformat() / "native.chk"
                    parent, _ = read_checkpoint(
                        previous, session["identity"], decision.expected_parent
                    )
                    require(parent["complete"], "PAPER_PARENT_INCOMPLETE")
                    base_cursor, parent_complete = parent["receipt"]["cursor"], True
                    if not (root / "native.chk").exists():
                        atomic(root / "native.chk", regular(previous))
                request = {
                    "session": config.model_dump(mode="json"),
                    "decision": json.loads(row["decision"]),
                    "close": json.loads(row["close_input"]),
                    "identity": session["identity"],
                    "engine_pin": json.loads(session["engine_pin"]),
                    "input_id": row["input_id"],
                    "base_cursor": base_cursor,
                    "parent_complete": parent_complete,
                    "parent_sha": decision.expected_parent,
                }
                request_path = root / "request.json"
                if request_path.exists():
                    require(regular(request_path) == canonical(request), "PAPER_REQUEST_CHANGED")
                else:
                    atomic(request_path, request)
                complete = False
                if (root / "native.chk").exists():
                    state, _ = read_checkpoint(root / "native.chk", session["identity"])
                    complete = state["complete"] and state["receipt"]["input_id"] == row["input_id"]
                if not complete:
                    with (root / ("native-" + token + ".log")).open("wb") as log:
                        process = spawn(request_path, log, serial, writer, fault)
                        if stage:
                            stage("spawned", process)
                        start = heartbeat = time.monotonic()
                        while process.poll() is None:
                            require(
                                time.monotonic() - start < 120 and log.tell() < 1024 * 1024,
                                "PAPER_NATIVE_RESOURCE_LIMIT",
                            )
                            if time.monotonic() - heartbeat >= 1:
                                self._heartbeat(sid, day, token)
                                heartbeat = time.monotonic()
                            time.sleep(0.1)
                        if process.returncode:
                            error_path = root / "error.json"
                            if error_path.exists():
                                error = json.loads(regular(error_path))
                                require(False, error["code"], error["message"])
                            require(
                                False,
                                "PAPER_NATIVE_INTERRUPTED",
                                f"Native worker exited {process.returncode}; inspect retained log and checkpoint",
                            )
                self._heartbeat(sid, day, token)
                proof, checkpoint_sha = read_checkpoint(root / "native.chk", session["identity"])
                receipt = completed_proof(proof, row, decision.expected_parent, base_cursor)
                report = {
                    "schema": "paper-report.v1",
                    "session_id": sid,
                    "day": day,
                    "intent_id": row["intent_id"],
                    "checkpoint_sha256": checkpoint_sha,
                    "native": receipt,
                    "summary": (
                        f"{day}：原生成交 {len(receipt['trades'])} 笔；"
                        f"现金 {receipt['account']['total_cash']}，"
                        f"权益 {receipt['account']['nav']}；对账通过；"
                        f"未完成目标 {len(receipt['target_gaps'])} 项。"
                        + (
                            "仅为条件历史研究重放。"
                            if config.data_mode == "historical_research"
                            else "仅为合成基础验收。"
                        )
                    ),
                    "real_scenario_acceptance": "pending",
                    "data_mode": config.data_mode,
                    "strategy_receipt": decision.strategy_receipt,
                    "strategy_receipt_sha256": decision.strategy_receipt_sha256,
                    "historical_pit_verified": False,
                    "execution_permission": False,
                }
                if stage:
                    stage("before_publish", process)
                if (root / "report.json").exists():
                    require(
                        regular(root / "report.json") == canonical(report), "PAPER_REPORT_CHANGED"
                    )
                else:
                    atomic(root / "report.json", report)
                with self.db() as db:
                    fresh = db.execute(
                        "SELECT * FROM paper_days WHERE session_id=? AND day=?", (sid, day)
                    ).fetchone()
                    head = db.execute(
                        "SELECT head FROM paper_sessions WHERE id=?", (sid,)
                    ).fetchone()[0]
                    require(
                        fresh["token"] == token and fresh["lease_until"] > time.time(),
                        "PAPER_FENCE_LOST",
                    )
                    require(head == decision.expected_parent, "PAPER_PARENT_CONFLICT")
                    db.execute(
                        "UPDATE paper_days SET status='committed',checkpoint_sha=?,report_sha=?,token=NULL,lease_until=NULL "
                        "WHERE session_id=? AND day=?",
                        (checkpoint_sha, digest(report), sid, day),
                    )
                    db.execute(
                        "UPDATE paper_sessions SET head=?,last_day=? WHERE id=?",
                        (checkpoint_sha, day, sid),
                    )
                    self.event(
                        db,
                        sid,
                        day,
                        "day_committed",
                        {"checkpoint_sha256": checkpoint_sha, "native_invoked": not complete},
                    )
                    if receipt["target_gaps"]:
                        self.event(
                            db,
                            sid,
                            day,
                            "alert",
                            {
                                "code": "PAPER_UNFILLED_TARGETS",
                                "target_gaps": receipt["target_gaps"],
                                "message": "Remainders cancelled; retry only on a new scheduled strategy intent.",
                            },
                        )
                return report
            except BaseException as exc:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                error = self.alert(sid, day, exc)
                with self.db() as db:
                    db.execute(
                        "UPDATE paper_days SET status='recovery_required',error=?,token=NULL,lease_until=NULL "
                        "WHERE session_id=? AND day=? AND token=?",
                        (canonical(error).decode(), sid, day, token),
                    )
                raise
