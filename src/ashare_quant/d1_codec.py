"""Frozen A数达 public D1/BR1 consumer; read-only, no execution admission.

Only documented Store/view/evidence methods supply data. Installed package bytes
are fingerprinted separately; storage paths/objects are never decoded here.
"""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from .contracts import Contract, DomainError
from .d1 import OwnerReceipt, Sha256, plan, plan_sha256
from .d1_binding import PublicEvidenceRef, SnapshotBinding
from .d1_br1 import profile, profile_sha256, refuse_br1_execution
from .research import verify_data_runtime, wire


def owner_pin() -> dict:
    root = files("ashare_quant")
    pin = json.loads(root.joinpath("d1-owner-pin.json").read_text())
    review_bytes = root.joinpath("d1-data-review.json").read_bytes()
    review = json.loads(review_bytes)
    if (hashlib.sha256(review_bytes).hexdigest() != pin["data_review_receipt_sha256"]
            or review["review_outcome"] != "PASS_WITH_FIXED_SCOPE"
            or review["data_versions"]["br1"]["wheel_sha256"] != pin.get("reviewed_data_wheel_sha256", pin["data_wheel_sha256"])
            or review["br1_dataset_id"] != pin["br1_dataset_id"]):
        mismatch("independent data review receipt")
    listing_bytes = root.joinpath("d1-listing-review.json").read_bytes()
    listing_review = json.loads(listing_bytes)
    if (hashlib.sha256(listing_bytes).hexdigest() != pin["listing_review_receipt_sha256"]
            or listing_review["outcome"] != "PASS_WITH_FIXED_SCOPE"
            or listing_review["wheel_sha256"] != pin["data_wheel_sha256"]
            or listing_review["listing_contract_sha256"] != pin["listing_contract_sha256"]):
        mismatch("independent dev3 listing review receipt")
    return pin


def public_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def public_dict(value):
    if isinstance(value, tuple):
        return [public_dict(v) for v in value]
    return wire(value.to_dict())


def mismatch(part: str):
    raise DomainError("BR1_PUBLIC_BINDING_MISMATCH", "Frozen public contract differs", 422,
                      details={"part": part})


class PublicDigest(Contract):
    name: str
    sha256: Sha256


class BR1BindRequest(Contract):
    mode: Literal["conditional_research"]
    backend_profile_sha256: Sha256


class FrozenBindingReport(Contract):
    scope: Literal["bound_public_snapshot_read_only"] = "bound_public_snapshot_read_only"
    dataset_id: Sha256
    manifest_sha256: Sha256
    owner_profile_sha256: Sha256
    backend_profile_sha256: Sha256
    installed_data_version: str
    installed_data_commit: str
    installed_data_tree: str
    installed_data_wheel_sha256: Sha256
    strict_binding: SnapshotBinding
    original_receipt: OwnerReceipt
    public_digests: tuple[PublicDigest, ...]
    verified_public_evidence: tuple[PublicEvidenceRef, ...]
    assumption_ids: tuple[str, ...]
    required_native_checks: tuple[str, ...]
    native_check_validation: Literal["NOT_RUN"] = "NOT_RUN"
    independent_data_admission: Literal["accepted_limited_public_api", "accepted_dev3_public_api_limited"] = "accepted_dev3_public_api_limited"
    data_review_receipt_sha256: Sha256
    listing_review_receipt_sha256: Sha256 | None = None
    listing_contract_sha256: Sha256 | None = None
    backend_binding_review: Literal["PENDING"] = "PENDING"
    execution_permission: Literal[False] = False
    strict_d1_admission: Literal["BLOCKED"] = "BLOCKED"
    br1_execution_admission: Literal["BLOCKED"] = "BLOCKED"
    result_grade: Literal["conditional_research"] = "conditional_research"
    historical_pit: Literal[False] = False


def validate_public_payload(payload: dict, pin: dict) -> None:
    """Identity/contract compatibility, not a new data-quality adjudicator."""
    if payload.keys() != pin["public_sha256"].keys():
        mismatch("public method set")
    for name, expected in pin["public_sha256"].items():
        if public_digest(payload[name]) != expected:
            mismatch(name)
    owner_profile = payload["br1.profile"]
    # raw_json is an explicitly documented PUBLIC field, not a package file read.
    raw = owner_profile["raw_json"]
    if hashlib.sha256(raw.encode()).hexdigest() != pin["owner_profile_sha256"]:
        mismatch("owner profile bytes")
    p = json.loads(raw)
    if (p["strict_plan_reference_sha256"] != plan_sha256()
            or p["backend_profile_reference_sha256"] != profile_sha256()
            or p["initial_cash"] != plan().initial_cash or p["initial_quantity"] != 0
            or p["initial_entitlements"] != 0):
        mismatch("backend profile references and initial conditions")
    intents = [i.model_dump(mode="json", exclude={"intent_id"}) for i in plan().intents]
    if p["intents"] != intents:
        mismatch("predefined intent sequence")
    if owner_profile["allowed_calls"] != [c.model_dump(mode="json") for c in profile().ad08_calls]:
        mismatch("AD08 full tuple set")
    if payload["br1.owner_receipt"]["strict_receipt"] != payload["strict.owner_receipt"]:
        mismatch("nested strict receipt")


@dataclass(frozen=True)
class BoundPublicSnapshot:
    """Immutable public DTO capture. No account, orders, run token or live handle."""

    public_json: str
    report: FrozenBindingReport

    def public(self, name: str):
        return json.loads(self.public_json)[name]  # Detached copies, not mutable stored state.

    def validate(self) -> None:
        pin = owner_pin()
        validate_public_payload(json.loads(self.public_json), pin)
        if (self.report.dataset_id != pin["br1_dataset_id"]
                or self.report.owner_profile_sha256 != pin["owner_profile_sha256"]
                or self.report.backend_profile_sha256 != profile_sha256()):
            mismatch("snapshot report")


class FrozenBR1Reader:
    """Explicit local read-only consumer. Caller JSON cannot supply an attestation."""

    def __init__(self, store_root: str | Path, *, dataset_id: str, mode: str,
                 backend_profile_sha256: str):
        pin = owner_pin()
        if mode != "conditional_research":
            raise DomainError("BR1_EXPLICIT_MODE_REQUIRED", "Only explicit conditional_research is prepared", 422)
        if dataset_id != pin["br1_dataset_id"] or backend_profile_sha256 != profile_sha256():
            mismatch("requested dataset/profile")
        self._root, self._pin = Path(store_root).expanduser().resolve(), pin

    def _store(self):
        verify_data_runtime()
        from ashare_data import Store
        return Store(self._root)

    @staticmethod
    def _call(operation):
        verify_data_runtime()
        from ashare_data import DataError
        try:
            return operation()
        except DataError as exc:
            raise DomainError(exc.code, exc.message, 422, details=wire(exc.details), origin="ashare-data") from exc
        except OSError as exc:
            raise DomainError("BR1_PUBLIC_READ_FAILED", "Public Store read failed", 422,
                              details={"type": type(exc).__name__}) from exc

    def snapshot(self) -> BoundPublicSnapshot:
        return self._call(self._snapshot)

    def _snapshot(self) -> BoundPublicSnapshot:
        pin, store = self._pin, self._store()
        strict = store.d1(pin["strict_dataset_id"])
        br1 = store.br1(pin["br1_dataset_id"], mode="conditional_research")
        facts = store.d1_facts(pin["facts_component_id"])
        public = {}
        for prefix, view, names in (
            ("strict", strict, ("descriptor", "owner_receipt", "instrument", "rules", "calendar", "bars", "statuses", "quality")),
            ("br1", br1, ("descriptor", "profile", "owner_receipt", "admission", "instrument", "rules", "calendar", "bars", "statuses", "modeled_events")),
        ):
            for name in names:
                public[prefix + "." + name] = public_dict(getattr(view, name)())
        public["facts.descriptor"] = public_dict(facts.descriptor())
        for call in profile().ad08_calls:
            day = call.trade_date.isoformat()
            public["br1.price_limits:" + day] = public_dict(br1.price_limits(call.trade_date))
            public["br1.prev_close:" + day] = public_dict(br1.adjusted_prev_close(**call.model_dump()))
        validate_public_payload(public, pin)
        evidence = tuple(PublicEvidenceRef.model_validate(r) for r in pin["evidence"])
        for ref in evidence:
            data = facts.evidence(ref.path)
            if (type(data) is not bytes or len(data) != ref.size
                    or hashlib.sha256(data).hexdigest() != ref.sha256):
                mismatch("evidence:" + ref.path)
        # Public reopen revalidates all nested manifests; detect drift during collection.
        again = store.br1(pin["br1_dataset_id"], mode="conditional_research")
        if public_dict(again.owner_receipt()) != public["br1.owner_receipt"]:
            mismatch("receipt changed during collection")
        current = store.d1(pin["strict_dataset_id"])
        if public_dict(current.owner_receipt()) != public["strict.owner_receipt"]:
            mismatch("strict receipt changed during collection")
        descriptor = public["strict.descriptor"]
        receipt = OwnerReceipt.model_validate(public["strict.owner_receipt"])
        binding = SnapshotBinding(
            dataset_id=descriptor["dataset_id"], manifest_sha256=descriptor["manifest_sha256"],
            price_dataset_id=descriptor["price_dataset_id"], facts_component_id=descriptor["facts_component_id"],
            owner_version=descriptor["owner_version"], reader_policy=descriptor["policy_version"],
            request_id=receipt.request_id, backend_plan_sha256=descriptor["plan_sha256"],
            security=descriptor["security"], scope_start=descriptor["scope_start"],
            scope_end=descriptor["scope_end"], evidence=evidence)
        report = FrozenBindingReport(
            dataset_id=pin["br1_dataset_id"], manifest_sha256=pin["br1_dataset_id"],
            owner_profile_sha256=pin["owner_profile_sha256"], backend_profile_sha256=profile_sha256(),
            installed_data_version=pin["data_version"], installed_data_commit=pin["data_commit"],
            installed_data_tree=pin["data_tree"], installed_data_wheel_sha256=pin["data_wheel_sha256"],
            data_review_receipt_sha256=pin["data_review_receipt_sha256"],
            listing_review_receipt_sha256=pin["listing_review_receipt_sha256"],
            listing_contract_sha256=pin["listing_contract_sha256"],
            strict_binding=binding, original_receipt=receipt, verified_public_evidence=evidence,
            public_digests=tuple(PublicDigest(name=k, sha256=v) for k, v in pin["public_sha256"].items()),
            assumption_ids=tuple(public["br1.admission"]["assumption_ids"]),
            required_native_checks=tuple(public["br1.admission"]["required_native_checks"]))
        return BoundPublicSnapshot(json.dumps(public, ensure_ascii=False, sort_keys=True), report)

    def strict_adjusted_prev_close(self, call):
        # Strict v1 expects ISO dates; its original refusal remains authoritative.
        return self._call(lambda: self._store().d1(self._pin["strict_dataset_id"])
                          .adjusted_prev_close(**call.model_dump(mode="json")))

    def owner_execution_refusal(self):
        def operation():
            self._store().br1(self._pin["br1_dataset_id"], mode="conditional_research").require_execution()
            refuse_br1_execution()  # A permissive owner response still cannot authorize a run.
        return self._call(operation)
