"""Factor manifests in the existing task table and lease protocol."""

import json

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, digest, require
from ashare_quant.tasks.factors.contracts import FactorJobView, FactorManifest
from ashare_quant.tasks.repository import Repository as Base


class Repository(Base):
    view_type = FactorJobView

    @staticmethod
    def manifest(row):
        try:
            return Repository._manifest(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "M2_JOB_IDENTITY_CONFLICT",
                "Stored factor task identity is malformed or belongs to another task type",
                409,
            ) from exc

    @staticmethod
    def _manifest(row):
        value = FactorManifest.model_validate_json(row["manifest"])
        require(value.job_id == row["job_id"])
        return value

    def _view(self, row):
        return super()._view(row).model_copy(update={"total_cursors": 1})
