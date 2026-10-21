"""Typed Python tasks using the same queue, leases and cancellation fencing."""

import json

from ashare_quant.storage.artifacts import canonical, digest, require
from ashare_quant.tasks.minute.contracts import PythonJobView, PythonManifest
from ashare_quant.tasks.repository import Repository as Base


class Repository(Base):
    view_type = PythonJobView

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
