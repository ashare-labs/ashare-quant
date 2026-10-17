"""Durable single-host control plane. Execution outputs are read-only projections."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ashare_quant.contracts import (
    DomainError,
    EngineStep,
    EventPage,
    ExecutionStatus,
    LogEntry,
    Manifest,
    RunView,
    StateView,
    StrategyVersion,
)
from ashare_quant.storage.compatibility import compatible_snapshot, require_compatible_database
from ashare_quant.storage.lifecycle import database_lock, file_identity, initialize_database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        with database_lock(self.path, exclusive=True):
            if not self.path.exists():
                initialize_database(self.path)
            require_compatible_database(self.path)
            self._identity = file_identity(self.path)

    @contextmanager
    def initialization(self):
        """Serialize validation and policy admission with all cooperating connections.

        The yielded connection belongs to a disposable copy. Only an accepted first
        policy may open the original through _connection while holding this lock.
        """
        with database_lock(self.path, exclusive=True):
            self._require_identity()
            with compatible_snapshot(self.path) as snapshot:
                yield snapshot

    def _require_identity(self):
        if file_identity(self.path) != self._identity:
            raise DomainError("DATABASE_FILE_CHANGED",
                              "Database was removed or replaced; stop this instance and inspect it", 409)

    @contextmanager
    def connection(self, *, transaction: bool = False):
        with database_lock(self.path):
            with self._connection(transaction=transaction) as db:
                yield db

    @contextmanager
    def _connection(self, *, transaction=False):
        self._require_identity()
        # Never recreate a removed database or perform DDL on an existing database.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                             timeout=10, isolation_level=None)
        try:
            self._require_identity()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if transaction:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if transaction:
                db.commit()
        except BaseException:
            if transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(db, run_id):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise DomainError("RUN_NOT_FOUND", run_id, 404)
        return row

    @staticmethod
    def _view(row) -> RunView:
        return RunView(**{k: row[k] for k in RunView.model_fields})

    @staticmethod
    def _log(db, run_id, message, level="INFO"):
        db.execute("INSERT INTO logs(run_id,observed_at,level,message) VALUES (?,?,?,?)",
                   (run_id, now(), level, message))

    def put_strategy(self, version: StrategyVersion) -> StrategyVersion:
        with self.connection(transaction=True) as db:
            db.execute("INSERT OR IGNORE INTO strategies VALUES (?,?)",
                       (version.version_id, canonical(version)))
            row = db.execute("SELECT body FROM strategies WHERE version_id=?",
                             (version.version_id,)).fetchone()
            return StrategyVersion.model_validate_json(row[0])

    def strategy(self, version_id: str) -> StrategyVersion:
        with self.connection() as db:
            row = db.execute("SELECT body FROM strategies WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise DomainError("STRATEGY_NOT_FOUND", version_id, 404)
        return StrategyVersion.model_validate_json(row[0])

    def strategies(self) -> list[StrategyVersion]:
        with self.connection() as db:
            return [StrategyVersion.model_validate_json(r[0]) for r in
                    db.execute("SELECT body FROM strategies ORDER BY version_id")]

    def create_run(self, manifest: Manifest) -> RunView:
        req = manifest.request
        with self.connection(transaction=True) as db:
            old = db.execute("SELECT * FROM runs WHERE idempotency_key=?",
                             (req.idempotency_key,)).fetchone()
            if old:
                if old["request_hash"] != digest(req):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Key already identifies another request")
                return self._view(old)
            active = db.execute("SELECT run_id FROM runs WHERE account_id=? AND status IN "
                                "('queued','running')", (req.account_id,)).fetchone()
            if active:
                raise DomainError("ACCOUNT_BUSY", f"Account already belongs to run {active[0]}")
            run_id, stamp = str(uuid.uuid4()), now()
            db.execute("""INSERT INTO runs
                (run_id,idempotency_key,request_hash,strategy_version_id,account_id,status,
                 manifest,created_at,updated_at) VALUES (?,?,?,?,?,'queued',?,?,?)""",
                (run_id, req.idempotency_key, digest(req), req.strategy_version_id,
                 req.account_id, canonical(manifest), stamp, stamp))
            if req.engine_id == "rqalpha":
                db.execute("UPDATE runs SET reconciliation_required=1 WHERE run_id=?", (run_id,))
            self._log(db, run_id, "Queued synthetic RQAlpha run; real market NOT_RUN" if req.engine_id == "rqalpha"
                      else "Queued synthetic fixture replay; no strategy or real market execution")
            return self._view(self._row(db, run_id))

    def accepted_request(self, request) -> RunView | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE idempotency_key=?",
                             (request.idempotency_key,)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != digest(request):
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key already identifies another request")
            return self._view(row)

    def release(self, run_id: str, token: str, reason: str, *, reconcile_only=False):
        """Admission refusal is recoverable and keeps the account reservation."""
        with self.connection(transaction=True) as db:
            self._fence(self._row(db, run_id), token)
            db.execute("UPDATE runs SET status='queued',error=?,lease_token=NULL,lease_until=NULL,"
                       "native_reconcile_only=MAX(native_reconcile_only,?),updated_at=? WHERE run_id=?",
                       (reason, int(reconcile_only), now(), run_id))
            self._log(db, run_id, reason, "ERROR")

    def renew(self, run_id: str, token: str, lease_seconds: float = 30):
        with self.connection(transaction=True) as db:
            self._fence(self._row(db, run_id), token)
            db.execute("UPDATE runs SET lease_until=?,updated_at=? WHERE run_id=?",
                       (time.time() + lease_seconds, now(), run_id))

    def mark_native_started(self, run_id: str, token: str):
        with self.connection(transaction=True) as db:
            self._fence(self._row(db, run_id), token)
            db.execute("UPDATE runs SET native_started=1,reconciliation_required=1 WHERE run_id=?", (run_id,))

    def complete_native(self, run_id: str, token: str, receipt: dict, *, native_complete=True):
        with self.connection(transaction=True) as db:
            row = self._row(db, run_id)
            self._fence(row, token)
            if row["cursor"] != len(receipt["engine_events"]):
                raise DomainError("PROJECTION_INCOMPLETE", "Native receipt has unprojected events")
            if not native_complete and not row["cancel_requested_at"]:
                raise DomainError("NATIVE_OPERATOR_REVIEW_REQUIRED", "Partial native state reconciled; request cancel or inspect before resuming")
            status = "cancelled" if row["cancel_requested_at"] else "succeeded"
            db.execute("INSERT INTO native_receipts VALUES (?,?)", (run_id, canonical(receipt)))
            db.execute("UPDATE runs SET status=?,error=NULL,lease_token=NULL,lease_until=NULL,"
                       "reconciliation_required=0,native_reconcile_only=0,updated_at=? WHERE run_id=?",
                       (status, now(), run_id))
            self._log(db, run_id, f"RQAlpha {status}; native checkpoint reconciled; real market NOT_RUN")

    def receipt(self, run_id: str) -> dict:
        with self.connection() as db:
            self._row(db, run_id)
            row = db.execute("SELECT body FROM native_receipts WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise DomainError("RECEIPT_NOT_READY", "No completed native engine receipt", 404)
            return json.loads(row[0])

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> EventPage:
        with self.connection() as db:
            self._row(db, run_id)
            rows = db.execute("SELECT body FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                              (run_id, after, limit + 1)).fetchall()
        items = [EngineStep.model_validate_json(row[0]) for row in rows[:limit]]
        return EventPage(items=items, next_after=items[-1].sequence if items else after,
                         has_more=len(rows) > limit)

    def execution_status(self, run_id: str) -> ExecutionStatus:
        with self.connection() as db:
            row = self._row(db, run_id)
            active = row["status"] == "running" and row["lease_until"] > time.time()
            return ExecutionStatus(run_id=run_id, status=row["status"],
                engine_id=json.loads(row["manifest"])["engine"]["engine_id"],
                supervisor_lease_active=active,
                lease_expires_at=datetime.fromtimestamp(row["lease_until"], timezone.utc) if row["lease_until"] else None,
                last_error=row["error"], projection_cursor=row["cursor"],
                cancel_requested_at=row["cancel_requested_at"],
                reconciliation_required=row["reconciliation_required"],
                native_reconcile_only=row["native_reconcile_only"],
                recovery="terminal" if row["status"] not in ("queued", "running") else
                "operator_reconciliation" if row["native_reconcile_only"] else
                "cancel_reconciliation" if row["cancel_requested_at"] else
                "worker_active" if active else "cli_worker_after_lease_expiry")

    def run(self, run_id: str) -> RunView:
        with self.connection() as db:
            return self._view(self._row(db, run_id))

    def runs(self) -> list[RunView]:
        with self.connection() as db:
            return [self._view(r) for r in db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def manifest(self, run_id: str) -> Manifest:
        with self.connection() as db:
            return Manifest.model_validate_json(self._row(db, run_id)["manifest"])

    def claim(self, run_id: str | None = None, *, lease_seconds: float = 30) -> tuple | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.connection(transaction=True) as db:
            if run_id is None:
                row = db.execute("""SELECT * FROM runs WHERE native_reconcile_only=0 AND
                    (status='queued' OR (status='running' AND lease_until<=?)) ORDER BY created_at LIMIT 1""",
                    (time.time(),)).fetchone()
                if row is None:
                    return None
            else:
                row = self._row(db, run_id)
            if row["status"] not in ("queued", "running"):
                raise DomainError("RUN_TERMINAL", "A terminal run cannot be restarted")
            if row["status"] == "running" and row["lease_until"] > time.time():
                raise DomainError("RUN_LEASED", "Run has an active worker lease")
            token = str(uuid.uuid4())
            db.execute("UPDATE runs SET status='running',lease_token=?,lease_until=?,updated_at=? "
                       "WHERE run_id=?", (token, time.time() + lease_seconds, now(), row["run_id"]))
            self._log(db, row["run_id"], "Worker claimed run" if row["status"] == "queued"
                      else "Expired worker lease recovered from committed checkpoint")
            return (row["run_id"], token,
                    json.loads(row["checkpoint"]) if row["checkpoint"] else None)

    @staticmethod
    def _fence(row, token):
        if (row["status"] != "running" or row["lease_token"] != token
                or row["lease_until"] <= time.time()):
            raise DomainError("STALE_WORKER", "Worker lease expired, was cancelled or replaced")

    def commit_step(self, run_id: str, token: str, step: EngineStep, *, lease_seconds=30):
        with self.connection(transaction=True) as db:
            row = self._row(db, run_id)
            self._fence(row, token)
            body = canonical(step)
            if step.sequence <= row["cursor"]:
                old = db.execute("SELECT body FROM events WHERE run_id=? AND sequence=?",
                                 (run_id, step.sequence)).fetchone()
                if old and old[0] == body:
                    return  # Exact retry does not post or log twice.
                raise DomainError("EVENT_CONFLICT", "Sequence already has different content")
            if step.sequence != row["cursor"] + 1:
                raise DomainError("EVENT_GAP", "Engine sequence must be contiguous")
            previous = db.execute("SELECT body FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                                  (run_id,)).fetchone()
            if previous:
                prior = EngineStep.model_validate_json(previous[0])
                if step.logical_at < prior.logical_at:
                    raise DomainError("CLOCK_REGRESSION", "Strategy logical clock cannot move backwards")
            try:
                db.execute("INSERT INTO events VALUES (?,?,?)", (run_id, step.sequence, body))
            except sqlite3.IntegrityError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY):
                    raise
                raise DomainError("DUPLICATE_ENGINE_EVENT", "Fill/action identity was already committed") from exc
            db.execute("UPDATE runs SET cursor=?,checkpoint=?,updated_at=?,lease_until=? WHERE run_id=?",
                       (step.sequence, canonical(step.checkpoint), now(), time.time() + lease_seconds,
                        run_id))
            name = "RQAlpha" if json.loads(row["manifest"])["engine"]["engine_id"] == "rqalpha" else "fixture"
            self._log(db, run_id, f"{name} event {step.sequence}: {step.message}")

    def finish(self, run_id: str, token: str, error: str | None = None, *, prestart_obstruction=False):
        with self.connection(transaction=True) as db:
            row = self._row(db, run_id)
            self._fence(row, token)
            if row["native_started"] or row["reconciliation_required"]:
                root = self.path.parent / "engines" / run_id
                if not (prestart_obstruction and error and not row["native_started"]
                        and row["cursor"] == 0 and not row["cancel_requested_at"] and root.is_file()):
                    raise DomainError("NATIVE_RECONCILIATION_REQUIRED", "Native facts cannot be terminalized by generic worker failure")
            db.execute("UPDATE runs SET status=?,error=?,updated_at=?,lease_token=NULL,lease_until=NULL,"
                       "reconciliation_required=0,native_reconcile_only=0 "
                       "WHERE run_id=?", ("failed" if error else "succeeded", error, now(), run_id))
            self._log(db, run_id, error or "Synthetic replay completed; real engine verification NOT_RUN",
                      "ERROR" if error else "INFO")

    def cancel(self, run_id: str) -> RunView:
        with self.connection(transaction=True) as db:
            row = self._row(db, run_id)
            if row["status"] not in ("queued", "running"):
                raise DomainError("RUN_TERMINAL", "Only active runs can be cancelled")
            if json.loads(row["manifest"])["engine"]["engine_id"] == "rqalpha":
                if row["cancel_requested_at"] is None:
                    db.execute("UPDATE runs SET cancel_requested_at=?,reconciliation_required=1,"
                               "native_reconcile_only=0,updated_at=? WHERE run_id=?", (now(), now(), run_id))
                    self._log(db, run_id, "Cancellation requested; account held until native writer stops and checkpoint is reconciled")
                else:
                    db.execute("UPDATE runs SET native_reconcile_only=0 WHERE run_id=?", (run_id,))
                return self._view(self._row(db, run_id))
            db.execute("UPDATE runs SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? "
                       "WHERE run_id=?", (now(), run_id))
            self._log(db, run_id, "Cancelled and fenced active worker")
            return self._view(self._row(db, run_id))

    def logs(self, run_id: str, after: int = 0) -> list[LogEntry]:
        with self.connection() as db:
            self._row(db, run_id)
            return [LogEntry(index=r["id"], **{k: r[k] for k in ("observed_at", "level", "message")})
                    for r in db.execute("SELECT * FROM logs WHERE run_id=? AND id>? ORDER BY id LIMIT 1000",
                                        (run_id, after))]

    def state(self, run_id: str) -> StateView:
        # Explicit read transaction: cursor and projection must come from one consistent snapshot.
        with self.connection() as db:
            db.execute("BEGIN")
            run = self._view(self._row(db, run_id))
            engine_id = json.loads(self._row(db, run_id)["manifest"])["engine"]["engine_id"]
            steps = [EngineStep.model_validate_json(r[0]) for r in db.execute(
                "SELECT body FROM events WHERE run_id=? ORDER BY sequence", (run_id,))]
            db.commit()
        orders, fills = {}, {}
        for step in steps:
            if step.order:
                orders[step.order.order_id] = step.order
            if step.fill:
                if step.fill.fill_id in fills:
                    raise DomainError("DUPLICATE_FILL", "Corrupt engine stream: duplicate fill identity")
                fills[step.fill.fill_id] = step.fill
        last = steps[-1] if steps else None
        return StateView(run=run, logical_at=last.logical_at if last else None,
                         trading_day=last.trading_day if last else None,
                         account=last.account if last else None,
                         orders=list(orders.values()), fills=list(fills.values()),
                         projection_complete=not run.reconciliation_required,
                         truth_source=engine_id, is_test_double=engine_id == "fixture-replay-v1")

    def backup(self, destination: str | Path):
        target = Path(destination).expanduser().resolve()
        if target == self.path or target.exists():
            raise DomainError("BACKUP_EXISTS", "Backup destination must be a new file")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids overwriting an existing backup in a concurrent invocation.
        with target.open("xb"):
            pass
        with self.connection() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
                active = dest.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running') "
                    "AND json_extract(manifest,'$.engine.engine_id')='rqalpha'").fetchone()[0]
            finally:
                dest.close()
        if active:
            target.unlink()  # Only the new exclusive file created by this invocation.
            raise DomainError("NATIVE_BACKUP_INCOMPLETE", "Active native runs need the engine checkpoint directory; "
                              "stop this project's workers and snapshot the entire state directory")
