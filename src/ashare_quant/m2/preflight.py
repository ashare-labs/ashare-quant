"""Non-executing M2 preparation checks. No engine/account/job creation."""

from typing import Literal

from ashare_quant.contracts import Contract, DomainError

from .catalogue import Catalogue
from .common import digest
from .contracts import BacktestRequest, Sha
from .execution_dates import execution_days


class PreflightReceipt(Contract):
    contract_version: Literal["m2.daily.v2"] = "m2.daily.v2"
    request_sha256: Sha
    catalogue_sha256: Sha
    contract_checks: Literal["passed_synthetic_contract", "passed_owner_codec_contract", "blocked"]
    contract_blockers: tuple[str, ...]
    execution_blockers: tuple[str, ...] = ("M2_NATIVE_INTEGRATION_PENDING", "M2_REAL_DATA_NOT_ADMITTED")
    execution_permission: Literal[False] = False
    account_created: Literal[False] = False
    job_created: Literal[False] = False
    selected_window_id: Sha | None = None
    trading_day_count: int = 0
    assumptions_profile_sha256: Sha | None = None
    original_unknowns: tuple[str, ...] = ()


def preflight(request: BacktestRequest, registry, catalogue: Catalogue) -> PreflightReceipt:
    blockers = []
    try:
        registry.get(request.strategy_version_id)
    except DomainError as exc:
        if exc.status != 404:
            raise
        blockers.append("M2_STRATEGY_NOT_FOUND")
    entry, window, profile, days = None, None, None, ()
    try:
        entry = catalogue.get(request.data_binding_id)
        window = entry.window(request.window_id)
        profile = entry.profile(window.window_id)
        days = execution_days(entry, request)
        if entry.evidence_kind == "observed":
            from .owner_wire import validate_binding
            validate_binding(entry)
        if request.assumptions.profile_sha256 != profile.profile_sha256:
            blockers.append("M2_ASSUMPTION_PROFILE_MISMATCH")
        blockers.extend(entry.unmet_requirements)
    except DomainError as exc:
        if exc.status != 422:
            raise
        blockers.append(exc.body()["code"])
    return PreflightReceipt(request_sha256=digest(request), catalogue_sha256=catalogue.identity,
        contract_checks="blocked" if blockers else ("passed_owner_codec_contract" if entry.evidence_kind == "observed" else "passed_synthetic_contract"),
        contract_blockers=tuple(dict.fromkeys(blockers)), selected_window_id=window.window_id if window else None,
        trading_day_count=len(days),
        assumptions_profile_sha256=profile.profile_sha256 if profile else None,
        original_unknowns=entry.original_unknowns if entry else ())


def refuse_submission():
    raise DomainError("M2_NATIVE_INTEGRATION_PENDING", "M2 submission is not implemented or admitted; no job or account was created", 422)
