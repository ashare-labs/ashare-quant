"""D1 design contracts and diagnostics only. No data reads, engine or queue."""

import hashlib
import json
from datetime import date
from importlib.resources import files
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from .contracts import SHANGHAI, Contract, DomainError

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
FactKey = Literal[
    "identity", "raw_bars", "calendar", "instrument", "trading_rules",
    "suspended", "st_status", "price_limits", "event_absence",
    "adjusted_prev_close", "owner_scope", "receipt", "historical_pit",
    "market_authenticity", "closing_queue", "personal_fees", "amount",
]


class Requirement(Contract):
    key: FactKey
    classification: Literal["required_fact", "declared_model_assumption", "not_used"]
    data_request: str
    condition: str


class PlannedIntent(Contract):
    intent_id: Literal["buy-20200103", "sell-20200106"]
    security: Literal["600000.XSHG"] = "600000.XSHG"
    at: AwareDatetime
    side: Literal["buy", "sell"]
    quantity: Literal[100] = 100
    order_style: Literal["market"] = "market"


class D1Plan(Contract):
    schema_version: Literal[1] = 1
    revision: Literal[2] = 2
    request_id: Literal["AQ-D1-600000-20200103-20200106-v2"]
    strategy_version: str
    model_version: str
    recorded_at: AwareDatetime
    logical_decision_cutoff: AwareDatetime
    scope_start: date
    scope_end: date
    price_dataset_id: Sha256
    initial_cash: Literal["10000.00"]
    initial_quantity: Literal[0]
    initial_entitlements: Literal[0]
    intents: tuple[PlannedIntent, PlannedIntent]
    requirements: tuple[Requirement, ...]
    model_assumptions: tuple[str, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def fixed_window(self):
        expected = (("buy-20200103", "2020-01-03T15:00:00+08:00", "buy"),
                    ("sell-20200106", "2020-01-06T15:00:00+08:00", "sell"))
        actual = tuple((i.intent_id, i.at.astimezone(SHANGHAI).isoformat(), i.side) for i in self.intents)
        if actual != expected or self.scope_start != date(2020, 1, 2) or self.scope_end != date(2020, 1, 7):
            raise ValueError("D1 v2 has a fixed window and fixed intent sequence")
        if self.logical_decision_cutoff.astimezone(SHANGHAI).isoformat() != "2020-01-02T23:59:59.999999+08:00":
            raise ValueError("D1 v2 decision cutoff is fixed")
        if len({r.key for r in self.requirements}) != len(self.requirements):
            raise ValueError("duplicate requirement")
        return self


def plan() -> D1Plan:
    return D1Plan.model_validate_json(files("ashare_quant").joinpath("d1-plan-v2.json").read_text())


def plan_sha256() -> str:
    payload = json.dumps(plan().model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class PlanView(Contract):
    plan: D1Plan
    plan_sha256: Sha256
    implementation: Literal["contract_skeleton_only"] = "contract_skeleton_only"
    execution_permission: Literal[False] = False
    historical_execution_admission: Literal["BLOCKED"] = "BLOCKED"
    d1_acceptance_status: Literal["NOT_RUN"] = "NOT_RUN"


def describe_plan() -> PlanView:
    return PlanView(plan=plan(), plan_sha256=plan_sha256())


class FactClaim(Contract):
    """Proposed owner-envelope projection; caller claims are never attestations."""

    key: FactKey
    conclusion: Literal["supported", "no_related_events", "events_present", "unknown", "unsupported"]
    evidence_status: str = Field(min_length=1, max_length=128)
    evidence_references: tuple[str, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)


class OwnerReceipt(Contract):
    """Coordination contract, not an implemented ashare-data public API."""

    owner: Literal["ashare-data"] = "ashare-data"
    owner_version: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=96)
    dataset_id: Sha256
    manifest_sha256: Sha256
    security: Literal["600000.XSHG"]
    scope_start: date
    scope_end: date
    facts: tuple[FactClaim, ...] = Field(max_length=17)

    @model_validator(mode="after")
    def bounds_and_keys(self):
        if self.scope_start > self.scope_end or len({f.key for f in self.facts}) != len(self.facts):
            raise ValueError("ordered scope and unique fact keys required")
        return self


class DataOwnerD1Adapter(Protocol):
    def read_receipt(self, dataset_id: str) -> OwnerReceipt:
        """Future binding to a frozen public owner API. No implementation yet."""
        ...


class D1CheckRequest(Contract):
    plan_sha256: Sha256
    owner_receipt: OwnerReceipt | None = None


class Diagnostic(Contract):
    key: FactKey
    classification: Literal["required_fact", "declared_model_assumption", "not_used"]
    result: Literal["missing", "insufficient_claim", "claim_present_unverified", "nonblocking"]
    owner_claim: FactClaim | None = None


class D1Check(Contract):
    plan_sha256: Sha256
    status: Literal["BLOCKED"] = "BLOCKED"
    execution_permission: Literal[False] = False
    owner_binding_verified: Literal[False] = False
    d1_acceptance_status: Literal["NOT_RUN"] = "NOT_RUN"
    blockers: tuple[str, ...]
    requirements: tuple[Diagnostic, ...]
    note: str = "Diagnostic claims only; no owner verification, engine, queue, or data reads"


def preflight(request: D1CheckRequest) -> D1Check:
    frozen = plan()
    if request.plan_sha256 != plan_sha256():
        raise DomainError("D1_PLAN_MISMATCH", "Use the frozen D1 v2 plan hash", 422)
    blockers = ["D1_EXECUTION_NOT_IMPLEMENTED", "ASHARE_DATA_D1_PUBLIC_BINDING_UNAVAILABLE"]
    receipt = request.owner_receipt
    if receipt and (receipt.request_id != frozen.request_id
                    or receipt.scope_start > frozen.scope_start or receipt.scope_end < frozen.scope_end):
        blockers.append("D1_OWNER_SCOPE_MISMATCH")
    claims = {f.key: f for f in receipt.facts} if receipt else {}
    diagnostics = []
    for required in frozen.requirements:
        claim = claims.get(required.key)
        if required.classification != "required_fact":
            outcome = "nonblocking"
        elif claim is None:
            outcome = "missing"
            blockers.append("D1_MISSING:" + required.key)
        elif (not claim.evidence_references or claim.evidence_status.lower() in {"unknown", "unsupported"}
              or claim.conclusion != ("no_related_events" if required.key == "event_absence" else "supported")):
            outcome = "insufficient_claim"
            blockers.append("D1_INSUFFICIENT:" + required.key)
        else:
            outcome = "claim_present_unverified"
        diagnostics.append(Diagnostic(key=required.key, classification=required.classification,
                                      result=outcome, owner_claim=claim))
    return D1Check(plan_sha256=plan_sha256(), blockers=tuple(blockers), requirements=tuple(diagnostics))


class PreviewRequest(Contract):
    logical_at: AwareDatetime
    phase: Literal["BEFORE_TRADING", "BAR", "AFTER_TRADING"]
    consumed_intents: tuple[Literal["buy-20200103", "sell-20200106"], ...] = Field(default=(), max_length=2)


class IntentPreview(Contract):
    plan_sha256: Sha256
    logical_at: AwareDatetime
    intent: PlannedIntent | None
    native_order_submitted: Literal[False] = False
    label: Literal["non_executing_intent_preview"] = "non_executing_intent_preview"


def preview(request: PreviewRequest) -> IntentPreview:
    """Only clock + immutable intent + explicit idempotency hints; no market context."""
    frozen = plan()
    at = request.logical_at.astimezone(SHANGHAI)
    if at <= frozen.logical_decision_cutoff:
        raise DomainError("D1_BEFORE_DECISION_CUTOFF", "Intent preview requires a later logical phase", 422)
    if not frozen.scope_start <= at.date() <= frozen.scope_end:
        raise DomainError("D1_PREVIEW_OUT_OF_SCOPE", "Outside the frozen preview window", 422)
    intent = next((i for i in frozen.intents if i.at == at and i.intent_id not in request.consumed_intents), None)
    return IntentPreview(plan_sha256=plan_sha256(), logical_at=at,
                         intent=intent if request.phase == "BAR" else None)


class NativeBoundarySkeleton:
    """Not registered with RQ. Trusted dispatcher preview never inspects native inputs.

    Native context can expose portfolio valuations; bar_dict and price APIs can expose
    current data. None are strategy inputs here. Native-hook isolation remains NOT_RUN.
    """

    def preview_callback(self, request: PreviewRequest, *, native_context=None, bar_dict=None,
                         current_snapshot=None, price_board=None, history_bars=None) -> IntentPreview:
        return preview(request)

    def handle_bar(self, context, bar_dict):
        refuse_execution()


def refuse_execution() -> None:
    raise DomainError("D1_EXECUTION_BLOCKED", "D1 contracts are non-executable; data admission and native binding are unavailable", 422)
