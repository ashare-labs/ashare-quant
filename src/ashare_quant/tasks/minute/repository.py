"""Typed Python tasks using the same queue, leases and cancellation fencing."""

import json

from ashare_quant.storage.artifacts import canonical, digest, require
from ashare_quant.tasks.minute.contracts import (
    PythonCreate,
    PythonJobView,
    PythonManifest,
    PythonScope,
)
from ashare_quant.tasks.repository import Repository as Base


class Repository(Base):
    view_type = PythonJobView

    def existing(self, request):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (request.idempotency_key,)
            ).fetchone()
        if row:
            # Acceptance pins this alias, so retries do not resolve a file that
            # may since have been deleted or replaced. Legacy IDs stay exact.
            frozen = json.loads(row["manifest"])
            if (frozen.get("schema_version") == "python-demand-job.v2"
                    and "submitted_strategy_id" in frozen["session"]):
                manifest = self.manifest(row)
                if request.strategy_id == manifest.session["submitted_strategy_id"]:
                    request = request.model_copy(update={"strategy_id": manifest.request.strategy_id})
            require(
                row["request_hash"] == digest(request), "M2_JOB_IDEMPOTENCY_CONFLICT",
                "Key already identifies another immutable request",
            )
            return dict(row)
        return None

    @staticmethod
    def manifest(row):
        try:
            value = PythonManifest.model_validate_json(row["manifest"])
        except ValueError:
            raw = json.loads(row["manifest"])
            req = raw.get("request", {})
            scope = raw.get("execution_scope", {})
            value = PythonManifest(
                schema_version="python-minute-job.v1",
                job_id=row["job_id"],
                request=PythonCreate(
                    environment_id=req.get("environment_id", "0" * 64),
                    name=req.get("name", row["job_id"]),
                    idempotency_key=row["idempotency_key"],
                    source=req.get("source", "# legacy task\n"),
                    parameters=req.get("parameters", {}),
                    frequency=scope.get("frequency", req.get("frequency", "1d")),
                    warmup_sessions=int(scope.get("warmup_sessions", req.get("warmup_sessions", 20))),
                    securities=tuple(scope.get("securities", req.get("securities", ()))),
                    start_date=scope.get("start_date", req.get("start_date", "2020-01-01")),
                    end_date=scope.get("end_date", req.get("end_date", "2020-01-01")),
                    initial_cash=float(req.get("initial_cash", 100000)),
                ),
                source_sha256=(
                    raw.get("source_sha256")
                    or raw.get("service_identity", {}).get("source_sha256", "0" * 64)
                ),
                service_identity=raw.get("service_identity", {}),
                execution_scope=PythonScope(
                    securities=tuple(scope.get("securities", req.get("securities", ()))),
                    trading_dates=tuple(scope.get("trading_dates", ())),
                    frequency=scope.get("frequency", req.get("frequency", "1d")),
                ),
                release=raw.get("release", {}),
                session=raw.get("session", {}),
                session_sha256=raw.get("session_sha256", "0" * 64),
                session_file_sha256=raw.get("session_file_sha256", "0" * 64),
                files=raw.get("files", {}),
            )
        require(value.job_id == row["job_id"])
        return value

    def _view(self, row):
        view = super()._view(row)
        return view.model_copy(
            update={"total_cursors": 1, "can_resume": False, "recovery_required": False}
        )
