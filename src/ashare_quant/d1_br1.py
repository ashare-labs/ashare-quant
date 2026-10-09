"""D1-BR1 conditional contract preparation. No admitted codec or native execution.

These normalized DTOs are backend contracts, not guessed upstream field names.
Rehearsals use explicit synthetic ports. Strict D1 admission remains unchanged.
"""

from datetime import date
from typing import Literal, Protocol

from pydantic import Field

from .contracts import Contract, DomainError
from .d1 import D1Check, D1CheckRequest, OwnerReceipt, Sha256, plan_sha256, preflight
from .d1_binding import SnapshotBinding, contract_digest
from .d1_native import AD08Call

AssumptionID = Literal["A-EQ", "A-NORMAL", "A-AD08"]
CONDITIONAL_KEYS = ("instrument", "price_limits", "event_absence", "adjusted_prev_close")
NATIVE_CHECKS = ("lot", "tick", "T+1", "price_limits", "suspended", "st_status", "volume_limits")


class Assumption(Contract):
    id: AssumptionID
    definition: str
    depends_on: tuple[AssumptionID, ...] = ()


class BR1Profile(Contract):
    profile_id: Literal["D1-BR1-v1"] = "D1-BR1-v1"
    strict_plan_sha256: Sha256
    official_bundle_manifest_sha256: Sha256
    policy_source_sha256: Sha256
    price_basis_start: date
    price_basis_end: date
    holding_start: date
    holding_end: date
    assumptions: tuple[Assumption, Assumption, Assumption]
    ad08_calls: tuple[AD08Call, AD08Call, AD08Call, AD08Call]
    required_native_checks: tuple[str, ...]
    result_grade: Literal["conditional_research"] = "conditional_research"
    evidence_status: Literal["bounded_corroborated_review"] = "bounded_corroborated_review"
    execution_permission: Literal[False] = False
    native_acceptance: Literal["NOT_RUN"] = "NOT_RUN"
    strict_d1_admission: Literal["BLOCKED"] = "BLOCKED"


def profile() -> BR1Profile:
    pairs = (("2020-01-02", "2019-12-31"), ("2020-01-03", "2020-01-02"),
             ("2020-01-06", "2020-01-03"), ("2020-01-07", "2020-01-06"))
    return BR1Profile(
        strict_plan_sha256=plan_sha256(),
        official_bundle_manifest_sha256="499cc2b4e2985f6597f1d8923fbf1878fcb148f4a0764835607cb3919b040d82",
        policy_source_sha256="24cf0c4778ab8966a3e0b760d9ee1830c38dd89658aec58c174a26356aad8c13",
        price_basis_start=date(2019, 12, 31), price_basis_end=date(2020, 1, 7),
        holding_start=date(2020, 1, 3), holding_end=date(2020, 1, 6),
        assumptions=(
            Assumption(id="A-EQ", definition="Bounded review assumes no omitted relevant ordinary-share rights or price adjustments in the required basis/holding/registration windows; initial shares and old entitlements are zero. A related event stops this profile."),
            Assumption(id="A-NORMAL", definition="Bounded historical inference assumes ordinary rules apply on the four dates, with no unrecognized special reference-price or listing exception. Source suspension/ST and native lot/tick/T+1/price/volume checks remain required."),
            Assumption(id="A-AD08", depends_on=("A-EQ",), definition="Only the four reviewed pre-close tuples are modeled equivalent to raw under A-EQ's no-adjustment assumption. This is not an observed factor of one or an independent fact.")),
        ad08_calls=tuple(AD08Call(security="600000.XSHG", trade_date=day, history_dt=prior,
                                  adjust_orig=day) for day, prior in pairs),
        required_native_checks=NATIVE_CHECKS)


def profile_sha256() -> str:
    return contract_digest(profile())


class BR1ProfileView(Contract):
    profile: BR1Profile
    profile_sha256: Sha256


def describe_br1_profile() -> BR1ProfileView:
    return BR1ProfileView(profile=profile(), profile_sha256=profile_sha256())


class SourceStatus(Contract):
    trade_date: date
    trading: Literal["active", "suspended", "unknown"]
    st: Literal["normal", "st", "unknown"]


class BR1Envelope(Contract):
    """Unverified normalized owner claims; original unknown facts are preserved."""

    profile_sha256: Sha256
    binding: SnapshotBinding
    original_receipt: OwnerReceipt
    official_bundle_manifest_sha256: Sha256
    assumptions_manifest_sha256: Sha256
    assumptions: tuple[AssumptionID, ...] = Field(max_length=3)
    ad08_depends_on: tuple[AssumptionID, ...] = Field(max_length=3)
    result_grade: Literal["conditional_research"]
    evidence_status: Literal["bounded_corroborated_review"]
    modeled_events_basis: Literal["assumed_no_relevant_events"]
    price_projection_basis: Literal["derived_under_declared_assumptions"]
    modeled_events: tuple[str, ...] = Field(max_length=1)
    related_event: Literal["none_identified", "identified", "unknown"]
    special_rule: Literal["none_identified", "identified", "unknown"]
    statuses: tuple[SourceStatus, ...] = Field(max_length=4)
    enabled_native_checks: tuple[str, ...] = Field(max_length=7)


class BR1Request(Contract):
    profile_sha256: Sha256
    owner_claim: BR1Envelope | None = None


class BR1Check(Contract):
    profile: BR1Profile
    profile_sha256: Sha256
    status: Literal["BLOCKED"] = "BLOCKED"
    execution_permission: Literal[False] = False
    upstream_binding_verified: Literal[False] = False
    claim_check: Literal["missing", "rejected", "consistent_unverified"]
    blockers: tuple[str, ...]
    strict_d1: D1Check


def br1_preflight(request: BR1Request) -> BR1Check:
    p = profile()
    if request.profile_sha256 != profile_sha256():
        raise DomainError("BR1_PROFILE_MISMATCH", "Use the frozen independent BR1 profile", 422)
    blockers = ["BR1_DATA_RELEASE_NOT_ADMITTED", "BR1_PUBLIC_CODEC_NOT_FROZEN", "BR1_NATIVE_EXECUTION_NOT_IMPLEMENTED"]
    claim = request.owner_claim
    strict = preflight(D1CheckRequest(plan_sha256=plan_sha256(),
                                      owner_receipt=claim.original_receipt if claim else None))
    if claim is None:
        blockers.append("BR1_OWNER_CLAIM_MISSING")
    else:
        b, r = claim.binding, claim.original_receipt
        if claim.profile_sha256 != profile_sha256():
            blockers.append("BR1_CLAIM_PROFILE_MISMATCH")
        if claim.official_bundle_manifest_sha256 != p.official_bundle_manifest_sha256:
            blockers.append("BR1_OFFICIAL_MANIFEST_MISMATCH")
        if (r.dataset_id, r.manifest_sha256, r.owner_version, r.request_id, r.security, r.scope_start, r.scope_end) != (
                b.dataset_id, b.manifest_sha256, b.owner_version, b.request_id, b.security, b.scope_start, b.scope_end):
            blockers.append("BR1_ORIGINAL_RECEIPT_IDENTITY_MISMATCH")
        if claim.assumptions != ("A-EQ", "A-NORMAL", "A-AD08") or claim.ad08_depends_on != ("A-EQ",):
            blockers.append("BR1_ASSUMPTION_DEPENDENCY_MISMATCH")
        facts = {f.key: f for f in r.facts}
        if any(k not in facts or facts[k].conclusion != "unknown" for k in CONDITIONAL_KEYS):
            blockers.append("BR1_ORIGINAL_UNKNOWN_FACTS_CHANGED")
        for item in strict.requirements:
            if (item.classification == "required_fact" and item.key not in CONDITIONAL_KEYS
                    and item.result != "claim_present_unverified"):
                blockers.append("BR1_REQUIRED_SOURCE_CLAIM:" + item.key)
        if claim.related_event != "none_identified" or claim.modeled_events:
            blockers.append("BR1_RELATED_EVENT_STOP")
        if claim.special_rule != "none_identified":
            blockers.append("BR1_SPECIAL_RULE_STOP")
        if (tuple(s.trade_date for s in claim.statuses) != tuple(c.trade_date for c in p.ad08_calls)
                or any(s.trading != "active" or s.st != "normal" for s in claim.statuses)):
            blockers.append("BR1_SOURCE_STATUS_STOP")
        if claim.enabled_native_checks != p.required_native_checks:
            blockers.append("BR1_NATIVE_CHECKS_CHANGED")
    return BR1Check(profile=p, profile_sha256=profile_sha256(), blockers=tuple(blockers), strict_d1=strict,
                    claim_check="missing" if claim is None else "rejected" if len(blockers) > 3 else "consistent_unverified")


class SyntheticConditionalPort(Protocol):
    """Normalized test port only. Does not assert upstream public method names."""

    def envelope(self) -> BR1Envelope: ...
    def adjusted_prev_close(self, **call) -> object: ...


def rehearse_br1_ad08(port: SyntheticConditionalPort, *, expected_envelope_sha256: str, call: AD08Call) -> object:
    if call not in profile().ad08_calls:
        raise DomainError("BR1_UNREVIEWED_CALL", "The complete tuple must match one of the four frozen calls", 422)
    before = port.envelope()
    if contract_digest(before) != expected_envelope_sha256:
        raise DomainError("BR1_BINDING_MISMATCH", "Conditional binding or assumptions manifest changed", 422)
    check = br1_preflight(BR1Request(profile_sha256=profile_sha256(), owner_claim=before))
    if check.claim_check != "consistent_unverified":
        raise DomainError("BR1_CONTRACT_REJECTED", "Conditional contract rehearsal refused", 422,
                          details={"blockers": check.blockers})
    value = port.adjusted_prev_close(**call.model_dump())
    if contract_digest(port.envelope()) != expected_envelope_sha256:
        raise DomainError("BR1_BINDING_CHANGED_DURING_READ", "Conditional binding changed during rehearsal", 422)
    return value  # Opaque test value; not converted to an RQ bar or promoted to a verified fact.


def refuse_br1_execution() -> None:
    raise DomainError("BR1_EXECUTION_BLOCKED", "Await a fixed data wheel, independent admission and native binding", 422,
                      details={"strict_d1_admission": "BLOCKED", "br1_native_acceptance": "NOT_RUN"})
