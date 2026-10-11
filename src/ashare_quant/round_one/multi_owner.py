"""Two-security binding to the same fixed public AD dataset; no secondary store."""

import copy
import json
from dataclasses import replace
from pathlib import Path

from ashare_quant.execution_six.owner import PublicOwner as SixOwner

from .owner import PublicOwner


class MultiOwner(PublicOwner):
    dataset_pin_path = Path(__file__).with_name("priority2-data-pin.json")
    worker_module = "ashare_quant.round_one.market_dev3_worker"

    @staticmethod
    def get_pin():
        return json.loads(Path(__file__).with_name("query-owner-pin.json").read_text())

    def validate_scope(self):
        available = {x["security"] for x in self.binding["coverage"]}
        if not set(self.case.securities) <= available:
            raise RuntimeError("MULTI_OWNER_SECURITY_SCOPE_MISSING")

    def descriptor(self):
        return dict(
            dataset_id=self.binding["dataset_id"],
            evidence_kind="real_source_observations",
            real_market_data=True,
            binding_sha256=self.binding_sha256,
            owner_pin=self.get_pin(),
            manifest=self.manifest,
            historical_pit=False,
            live_execution_permission=False,
            quality="conditional_sparse_research",
            conditions=self.manifest["limitations"],
            gaps=self.binding["gaps"],
        )

    def read(self, operation, logical_at, **parameters):
        sid = parameters.pop("security", self.case.security)
        if sid not in self.case.securities:
            raise RuntimeError("MULTI_OWNER_SECURITY_OUT_OF_SCOPE")
        # A separate immutable request context, not a mutable current-security cursor.
        bound = copy.copy(self)
        bound.case = replace(self.case, security=sid)
        return SixOwner.read(bound, operation, logical_at, **parameters)
