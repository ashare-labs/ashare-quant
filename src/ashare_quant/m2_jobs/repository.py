"""Task control operations over an already verified database, no account ledger."""

import json
import time
import uuid

from ashare_quant.contracts import DomainError
from ashare_quant.store import now

from .contracts import JobCreate, JobManifest, JobView, ResultReference
from .files import canonical, digest, require
from .policy import ServicePolicy, ServiceStatus

LEASE_SECONDS = 15


class Repository:
    def __init__(self, store, policy=None):
        self.store = store
        with store.initialization() as snapshot:
            row = snapshot.execute("SELECT policy,policy_sha256 FROM m2_job_service WHERE singleton=1").fetchone()
            if row:
                try:
                    accepted = ServicePolicy.model_validate_json(row[0])
                except ValueError as exc:
                    raise DomainError("M2_JOB_POLICY_CHANGED", "Stored service policy is invalid") from exc
                require(digest(accepted) == row[1], "M2_JOB_POLICY_CHANGED", "Stored service policy differs")
                require(policy is None or policy == accepted, "M2_JOB_POLICY_CHANGED",
                        "Use this database's fixed service policy; choose a new database to change limits")
                self.policy = accepted
            else:
                self.policy = policy or ServicePolicy()
                # The exclusive initialization lock still protects the preflight and
                # first admission. Existing policies never open the original to write.
                with store._connection(transaction=True) as db:
                    db.execute("INSERT INTO m2_job_service VALUES(1,?,?)",
                               (canonical(self.policy).decode(), digest(self.policy)))

    def capacity(self, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.capacity(connection)
        pending = db.execute("SELECT COUNT(*) FROM m2_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        if pending >= self.policy.max_pending:
            raise DomainError("M2_JOB_QUEUE_FULL", "Pending task capacity reached; retry after completion or cancellation", 429,
                              details={"max_pending": self.policy.max_pending})

    def status(self):
        with self.store.connection() as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM m2_jobs GROUP BY status")}
            recovery = db.execute("SELECT COUNT(*) FROM m2_jobs WHERE status IN ('running','cancelling') AND COALESCE(lease_until,0)<=?", (time.time(),)).fetchone()[0]
        return ServiceStatus(policy=self.policy, policy_sha256=digest(self.policy), counts=counts,
                             recovery_required=recovery, available_pending=max(0, self.policy.max_pending - sum(counts.get(k, 0) for k in ('queued','running','cancelling'))))

    @staticmethod
    def _log(db, job_id, event, details=None):
        db.execute(
            "INSERT INTO m2_job_logs(job_id,at,event,details) VALUES(?,?,?,?)",
            (job_id, now(), event, canonical(details or {}).decode()),
        )

    @staticmethod
    def _get(db, job_id):
        row = db.execute("SELECT * FROM m2_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise DomainError("M2_JOB_NOT_FOUND", "Unknown finite research task", 404)
        return dict(row)

    def row(self, job_id):
        with self.store.connection() as db:
            return self._get(db, job_id)

    @staticmethod
    def manifest(row):
        try:
            return Repository._manifest(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("M2_JOB_IDENTITY_CONFLICT", "Stored task identity is malformed", 409) from exc

    @staticmethod
    def _manifest(row):
        value = JobManifest.model_validate_json(row["manifest"])
        require(
            digest(value) == row["manifest_sha256"]
            and value.job_id == row["job_id"]
            and digest(value.request) == row["request_hash"]
            and value.request == JobCreate.model_validate_json(row["request"])
            and row["account_id"] == "bt-" + value.session["run_id"]
        )
        return value

    def view(self, row):
        try:
            return self._view(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("M2_JOB_STATE_INVALID", "Stored task state requires operator inspection", 409) from exc

    def _view(self, row):
        manifest = self.manifest(row)
        expired = (
            row["status"] in {"running", "cancelling"} and (row["lease_until"] or 0) <= time.time()
        )
        return JobView(
            job_id=row["job_id"],
            request=manifest.request,
            status=row["status"],
            account_id=row["account_id"],
            manifest_sha256=row["manifest_sha256"],
            session_sha256=manifest.session_sha256,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attempt_count=row["attempt_count"],
            committed_cursor=row["committed_cursor"],
            total_cursors=len(manifest.execution_scope.trading_dates) * 3,
            checkpoint_complete=bool(row["checkpoint_complete"]),
            cancellation_requested_at=row["cancellation_requested_at"],
            recovery_required=expired,
            can_resume=(row["status"] in {"failed", "cancelled"} or expired),
            result=ResultReference.model_validate_json(row["result"]) if row["result"] else None,
            error=json.loads(row["error"]) if row["error"] else None,
        )

    def existing(self, request):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM m2_jobs WHERE idempotency_key=?", (request.idempotency_key,)
            ).fetchone()
        if row:
            require(
                row["request_hash"] == digest(request),
                "M2_JOB_IDEMPOTENCY_CONFLICT",
                "Key already identifies another immutable request",
            )
            return dict(row)
        return None

    def insert(self, manifest):
        stamp = now()
        with self.store.connection(transaction=True) as db:
            self.capacity(db)
            db.execute(
                """INSERT INTO m2_jobs(job_id,idempotency_key,request_hash,request,manifest,
                manifest_sha256,account_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    manifest.job_id,
                    manifest.request.idempotency_key,
                    digest(manifest.request),
                    canonical(manifest.request).decode(),
                    canonical(manifest).decode(),
                    digest(manifest),
                    "bt-" + manifest.session["run_id"],
                    stamp,
                    stamp,
                ),
            )
            self._log(
                db,
                manifest.job_id,
                "queued",
                {
                    "manifest_sha256": digest(manifest),
                    "owner_preflight": "accepted_conditional_only",
                    "service_policy_sha256": digest(self.policy),
                    "service_policy": self.policy.model_dump(mode="json"),
                },
            )
        return self.row(manifest.job_id)

    def rows(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM m2_jobs ORDER BY created_at,rowid")]

    def candidates(self):
        with self.store.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT job_id FROM m2_jobs WHERE status='queued' OR "
                    "(status IN ('running','cancelling') AND lease_until<=? AND error IS NULL) ORDER BY created_at,rowid",
                    (time.time(),),
                )
            ]

    def cancel(self, job_id):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return row
            status = "cancelled" if row["status"] == "queued" else "cancelling"
            db.execute(
                "UPDATE m2_jobs SET status=?,cancellation_requested_at=COALESCE(cancellation_requested_at,?),updated_at=? WHERE job_id=?",
                (status, now(), now(), job_id),
            )
            if row["status"] != "cancelling":
                self._log(db, job_id, "cancellation_requested", {"status": status})
        return self.row(job_id)

    def resume(self, job_id, proof=None):
        # Caller must hold the task writer lock. An expired lease alone is not
        # proof that the old native child is gone.
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            if row["status"] == "queued":
                return row
            require(
                row["status"] in {"failed", "cancelled"}
                or (
                    row["status"] in {"running", "cancelling"} and row["lease_until"] <= time.time()
                ),
                "M2_JOB_NOT_RESUMABLE",
                "Task is still active or already succeeded",
            )
            if row["status"] in {"failed", "cancelled"}:
                self.capacity(db)
            db.execute(
                "UPDATE m2_jobs SET status='queued',lease_token=NULL,lease_until=NULL,"
                "cancellation_requested_at=NULL,error=NULL,result=NULL,updated_at=? WHERE job_id=?",
                (now(), job_id),
            )
            if proof:
                db.execute(
                    "UPDATE m2_jobs SET committed_cursor=?,checkpoint_complete=?,checkpoint_sha256=? WHERE job_id=?",
                    (proof["cursor"], int(proof["complete"]), proof["checkpoint_sha256"], job_id),
                )
            self._log(
                db,
                job_id,
                "resume_queued",
                {"prior_status": row["status"], "committed_cursor": row["committed_cursor"],
                 "attempt_base": row["attempt_count"], "granted_attempts": self.policy.max_attempts},
            )
        return self.row(job_id)

    def attempt_limit(self, db, row):
        """Explicit resume renews a bounded budget without erasing attempt history."""
        grant = db.execute(
            "SELECT details FROM m2_job_logs WHERE job_id=? AND event='resume_queued' "
            "ORDER BY sequence DESC LIMIT 1", (row["job_id"],)
        ).fetchone()
        try:
            base = json.loads(grant[0]).get("attempt_base", 0) if grant else 0
            require(type(base) is int and 0 <= base <= row["attempt_count"],
                    "M2_JOB_STATE_INVALID", "Invalid recovery attempt grant")
        except (ValueError, AttributeError, TypeError) as exc:
            raise DomainError("M2_JOB_STATE_INVALID", "Invalid recovery attempt grant") from exc
        return base + self.policy.max_attempts

    def claim(self, job_id):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            eligible = row["status"] == "queued" or (
                row["status"] in {"running", "cancelling"} and row["lease_until"] <= time.time()
            )
            if not eligible:
                return None
            if row["attempt_count"] >= self.attempt_limit(db, row):
                error = {"code": "M2_JOB_ATTEMPT_LIMIT", "message": "Automatic attempt budget exhausted; explicitly resume after addressing the failure"}
                db.execute("UPDATE m2_jobs SET status='failed',lease_token=NULL,lease_until=NULL,error=?,updated_at=? WHERE job_id=?",
                           (canonical(error).decode(), now(), job_id))
                self._log(db, job_id, "attempt_limit", error)
                return None
            token = uuid.uuid4().hex
            status = "cancelling" if row["cancellation_requested_at"] else "running"
            db.execute(
                "UPDATE m2_jobs SET status=?,lease_token=?,lease_until=?,attempt_count=attempt_count+1,updated_at=? WHERE job_id=?",
                (status, token, time.time() + LEASE_SECONDS, now(), job_id),
            )
            self._log(
                db,
                job_id,
                "writer_claimed",
                {
                    "attempt": row["attempt_count"] + 1,
                    "service_policy_sha256": digest(self.policy),
                    "recovered_expired_lease": row["status"] != "queued",
                },
            )
        return self.row(job_id)

    def starting(self, job_id, token):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            require(
                row["lease_token"] == token and row["lease_until"] > time.time(),
                "M2_JOB_STALE_WORKER",
            )
            db.execute("UPDATE m2_jobs SET checkpoint_sha256=NULL WHERE job_id=?", (job_id,))
            self._log(db, job_id, "native_child_starting", {"attempt": row["attempt_count"]})

    def heartbeat(self, job_id, token):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            require(
                row["lease_token"] == token
                and row["status"] in {"running", "cancelling"}
                and row["lease_until"] > time.time(),
                "M2_JOB_STALE_WORKER",
                "Worker no longer owns a valid lease",
            )
            db.execute(
                "UPDATE m2_jobs SET lease_until=? WHERE job_id=?",
                (time.time() + LEASE_SECONDS, job_id),
            )
            return bool(row["cancellation_requested_at"])

    def child_started(self, job_id, token, pid):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            require(
                row["lease_token"] == token and row["lease_until"] > time.time(),
                "M2_JOB_STALE_WORKER",
            )
            self._log(
                db,
                job_id,
                "native_child_started",
                {
                    "attempt": row["attempt_count"],
                    "pid": pid,
                    "fence_sha256": digest(token),
                    "manifest_sha256": row["manifest_sha256"],
                    "pid_is_signal_authority": False,
                },
            )

    def finish(self, job_id, token, *, proof=None, result=None, error=None):
        with self.store.connection(transaction=True) as db:
            row = self._get(db, job_id)
            require(
                row["lease_token"] == token
                and row["status"] in {"running", "cancelling"}
                and row["lease_until"] > time.time(),
                "M2_JOB_STALE_WORKER",
                "Stale worker cannot publish",
            )
            status = (
                "cancelled"
                if row["cancellation_requested_at"]
                else ("succeeded" if result else "failed")
            )
            if row["cancellation_requested_at"] and proof is None:
                status = "cancelling"
                error = error or {
                    "code": "M2_JOB_RECONCILIATION_REQUIRED",
                    "message": "Stopped writer has no verified checkpoint proof yet",
                }
            if status != "succeeded":
                result = None
            db.execute(
                """UPDATE m2_jobs SET status=?,lease_token=NULL,lease_until=?,updated_at=?,
                committed_cursor=?,checkpoint_complete=?,checkpoint_sha256=?,result=?,error=? WHERE job_id=?""",
                (
                    status,
                    0 if status == "cancelling" else None,
                    now(),
                    proof["cursor"] if proof else row["committed_cursor"],
                    int(proof["complete"]) if proof else row["checkpoint_complete"],
                    proof["checkpoint_sha256"] if proof else row["checkpoint_sha256"],
                    canonical(result).decode() if result else None,
                    canonical(error).decode() if error else None,
                    job_id,
                ),
            )
            self._log(
                db,
                job_id,
                status,
                {
                    "committed_cursor": proof["cursor"] if proof else row["committed_cursor"],
                    "checkpoint_complete": proof["complete"]
                    if proof
                    else bool(row["checkpoint_complete"]),
                    "result_published": result is not None,
                    "error": error,
                },
            )
        return self.row(job_id)

    def logs(self, job_id):
        with self.store.connection() as db:
            self._get(db, job_id)
            return [
                dict(
                    sequence=r["sequence"],
                    at=r["at"],
                    event=r["event"],
                    details=json.loads(r["details"]),
                )
                for r in db.execute(
                    "SELECT * FROM m2_job_logs WHERE job_id=? ORDER BY sequence", (job_id,)
                )
            ]
