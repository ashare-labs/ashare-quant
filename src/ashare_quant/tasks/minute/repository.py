"""Typed Python tasks using the same queue, leases and cancellation fencing."""

import json

from ashare_quant.storage.artifacts import canonical, digest, require
from ashare_quant.tasks.minute.contracts import PythonJobView, PythonManifest
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
        value = PythonManifest.model_validate_json(row["manifest"])
        require(
            digest(value) == row["manifest_sha256"]
            and value.job_id == row["job_id"]
            and digest(value.request) == row["request_hash"]
            and value.request.model_dump(mode="json") == json.loads(row["request"])
            and canonical(value).decode() == row["manifest"]
            and row["account_id"] == "bt-" + value.session["run_id"]
        )
        return value

    def _view(self, row):
        view = super()._view(row)
        return view.model_copy(update={"can_resume": False, "total_cursors": 1})
