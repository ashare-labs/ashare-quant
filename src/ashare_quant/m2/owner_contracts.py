"""Immutable transport for public owner documents; no owner import or raw data IO."""

from typing import Literal

from pydantic import Field

from ashare_quant.contracts import Contract


class OwnerDocuments(Contract):
    query: str
    result: str
    sources: str
    evidence: str
    authorization: str

    def stable(self):
        return self.query, self.result, self.sources, self.evidence


class OwnerBinding(Contract):
    release_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    descriptor: str
    window_plan: str
    envelope: str
    assumption_refs: tuple[str, ...]
    owner_window_id: Literal["m2a", "w1", "w2"]
    review_status: Literal["PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"] = "PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"
    execution_permission: Literal[False] = False
