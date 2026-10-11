"""Immutable transport for public owner documents; no owner import or raw data IO."""

from typing import Literal

from pydantic import Field

from ashare_quant.contracts import Contract

from .common import digest


class OwnerDocuments(Contract):
    query: str
    result: str
    sources: str
    evidence: str
    authorization: str

    def stable(self):
        return self.query, self.result, self.sources, self.evidence


class OwnerBinding(Contract):
    policy_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    owner_runtime_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    descriptor: str
    window_plan: str
    envelope: str
    assumption_refs: tuple[str, ...]
    owner_window_id: Literal["m2a", "w1", "w2"]
    review_status: Literal["local_contract_verified_not_independently_reviewed"] = (
        "local_contract_verified_not_independently_reviewed"
    )
    execution_permission: Literal[False] = False

    @property
    def identity(self):
        return digest(self)
