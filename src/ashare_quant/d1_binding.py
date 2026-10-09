"""Public-boundary contract rehearsal. No admitted D1 release or real codec exists.

The port method names follow the captured upstream draft. The normalized headers
below are backend contracts, NOT a claim about upstream dataclass field names.
Only explicit synthetic ports/codecs are used until an independently admitted
data wheel and its public schema are supplied. No paths or URLs are opened here.
"""

import hashlib
import json
from datetime import date
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .contracts import Contract, DomainError
from .d1 import D1Check, D1CheckRequest, OwnerReceipt, Sha256, plan, plan_sha256, preflight


class PublicEvidenceRef(Contract):
    path: str = Field(min_length=1, max_length=256)
    sha256: Sha256
    size: int = Field(strict=True, ge=0, le=32 * 1024 * 1024)

    @model_validator(mode="after")
    def component_relative_path(self):
        p = PurePosixPath(self.path)
        if (not p.parts or p.is_absolute() or str(p) != self.path or ".." in p.parts
                or "\\" in self.path or ":" in self.path or "\x00" in self.path):
            raise ValueError("Evidence is a canonical relative public component key, never a URL or OS path")
        return self


class SnapshotBinding(Contract):
    """Normalized binding identity. Real upstream decoding remains unimplemented."""

    dataset_id: Sha256
    manifest_sha256: Sha256
    price_dataset_id: Sha256
    facts_component_id: Sha256
    owner_version: str = Field(min_length=1, max_length=64)
    reader_policy: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=96)
    backend_plan_sha256: Sha256
    security: Literal["600000.XSHG"] = "600000.XSHG"
    scope_start: date
    scope_end: date
    evidence: tuple[PublicEvidenceRef, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def fixed_scope(self):
        frozen = plan()
        if (self.request_id != frozen.request_id or self.backend_plan_sha256 != plan_sha256()
                or self.price_dataset_id != frozen.price_dataset_id
                or self.scope_start != frozen.scope_start or self.scope_end != frozen.scope_end):
            raise ValueError("Binding must reference the exact frozen D1 v2 plan and scope")
        if len({r.path for r in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate public evidence key")
        return self


class PublicD1View(Protocol):
    def descriptor(self) -> object: ...
    def owner_receipt(self) -> object: ...
    def require_fact(self, key: str) -> object: ...
    def require_execution(self, *, plan_sha256: str) -> object: ...
    def adjusted_prev_close(self, **call) -> object: ...


class PublicFactsComponent(Protocol):
    def evidence(self, path: str) -> bytes: ...


class PublicD1Store(Protocol):
    def d1(self, dataset_id: str) -> PublicD1View: ...
    def d1_facts(self, component_id: str) -> PublicFactsComponent: ...


class DraftPublicCodec(Protocol):
    """Tests supply a synthetic codec. There is no real field-name decoder yet.

    A future admitted codec must translate public DataError into DomainError while
    preserving code/message/details/origin. It must not inspect private storage.
    """

    def header(self, public_object: object) -> SnapshotBinding: ...
    def receipt(self, public_object: object) -> OwnerReceipt: ...


class BindingProof(Contract):
    identity: SnapshotBinding
    identity_sha256: Sha256
    receipt_sha256: Sha256
    diagnostics: D1Check
    verified_public_evidence_keys: tuple[str, ...]
    scope: Literal["synthetic_contract_rehearsal"] = "synthetic_contract_rehearsal"
    upstream_release_verified: Literal[False] = False
    execution_permission: Literal[False] = False


def contract_digest(value: Contract) -> str:
    return hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def open_admitted_d1(*, store_alias: str, dataset_id: str):
    """Hard stop BEFORE import, filesystem access or opening an upstream store."""
    raise DomainError("D1_OWNER_RELEASE_NOT_ADMITTED",
                      "No frozen D1 data wheel, public codec and independent admission have been adopted", 422,
                      details={"data_release_pin": None, "execution_permission": False})


class ContractRehearsalBinding:
    """Only for explicitly supplied contract doubles; never a real-data run token.

    A successful hash comparison is byte/identity consistency, not market truth.
    Existing control state, native accounts and recovery are deliberately untouched.
    """

    def __init__(self, store: PublicD1Store, codec: DraftPublicCodec, expected: SnapshotBinding):
        self._store, self._codec, self._expected = store, codec, expected

    def _same_header(self, view: PublicD1View) -> None:
        actual = self._codec.header(view.descriptor())
        if actual != self._expected:
            raise DomainError("D1_BINDING_IDENTITY_MISMATCH", "Public snapshot identity changed", 422)

    def inspect(self) -> BindingProof:
        view = self._store.d1(self._expected.dataset_id)
        self._same_header(view)
        receipt = self._codec.receipt(view.owner_receipt())
        e = self._expected
        if (receipt.dataset_id, receipt.manifest_sha256, receipt.owner_version, receipt.request_id,
            receipt.security, receipt.scope_start, receipt.scope_end) != (
                e.dataset_id, e.manifest_sha256, e.owner_version, e.request_id,
                e.security, e.scope_start, e.scope_end):
            raise DomainError("D1_RECEIPT_IDENTITY_MISMATCH", "Receipt is not bound to the expected public snapshot", 422)
        component = self._store.d1_facts(e.facts_component_id)
        for ref in e.evidence:
            value = component.evidence(ref.path)  # Public component method only.
            if (type(value) is not bytes or len(value) != ref.size
                    or hashlib.sha256(value).hexdigest() != ref.sha256):
                raise DomainError("D1_EVIDENCE_MISMATCH", "Public evidence bytes differ from the expected reference", 422,
                                  details={"component_id": e.facts_component_id, "key": ref.path})
        # Re-read both after evidence calls; a mutable port must not swap versions mid-inspection.
        self._same_header(view)
        if self._codec.receipt(view.owner_receipt()) != receipt:
            raise DomainError("D1_RECEIPT_CHANGED", "Owner receipt changed during inspection", 422)
        return BindingProof(identity=e, identity_sha256=contract_digest(e), receipt_sha256=contract_digest(receipt),
                            diagnostics=preflight(D1CheckRequest(plan_sha256=plan_sha256(), owner_receipt=receipt)),
                            verified_public_evidence_keys=tuple(ref.path for ref in e.evidence))

    def read_required(self, key: str, method: str, **kwargs) -> object:
        """Bounded AD08 contract call, never a dynamic arbitrary method bridge."""
        if (key, method) != ("adjusted_prev_close", "adjusted_prev_close"):
            raise DomainError("D1_NATIVE_MAPPING_NOT_FROZEN", "Only the AD08 call contract is prepared", 422)
        before = self.inspect()
        view = self._store.d1(self._expected.dataset_id)
        self._same_header(view)
        view.require_fact(key)  # Owner decides availability; preserve its refusal.
        diagnostic = next(r for r in before.diagnostics.requirements if r.key == key)
        if diagnostic.result != "claim_present_unverified":
            raise DomainError("D1_OWNER_CONTRACT_CONFLICT", "Owner gate conflicts with its bound field conclusion", 422)
        value = view.adjusted_prev_close(**kwargs)
        after = self.inspect()
        if (after.identity_sha256, after.receipt_sha256) != (before.identity_sha256, before.receipt_sha256):
            raise DomainError("D1_BINDING_CHANGED_DURING_READ", "Public binding changed during a native-call rehearsal", 422)
        return value

    def require_execution(self) -> None:
        self.inspect()
        view = self._store.d1(self._expected.dataset_id)
        self._same_header(view)
        view.require_execution(plan_sha256=plan_sha256())  # Preserve owner error, never downgrade.
        # Even an accidentally permissive or forged port cannot unlock native execution.
        raise DomainError("D1_EXECUTION_BLOCKED", "Contract rehearsal cannot authorize native execution", 422)
