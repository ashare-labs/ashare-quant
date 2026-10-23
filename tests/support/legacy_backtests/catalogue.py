"""Fixed scoped metadata only. The production catalogue is deliberately empty."""

from pydantic import Field, model_validator

from ashare_quant.contracts import Contract, DomainError
from ashare_quant.storage.canonical import digest, require
from tests.support.legacy_backtests.contracts import (
    BacktestRequest,
    DataBinding,
    DecisionContext,
    ModelProfile,
    WindowPlan,
)
from tests.support.legacy_backtests.execution_dates import execution_days
from tests.support.legacy_provider.owner_contracts import OwnerBinding


class PreparationEntry(Contract):
    binding: DataBinding
    windows: tuple[WindowPlan, ...] = Field(min_length=1)
    profiles: tuple[ModelProfile, ...] = Field(min_length=1)
    evidence_kind: str = Field(pattern="^(synthetic|observed)$")
    owner_binding: OwnerBinding | None = None
    unmet_requirements: tuple[str, ...] = ()
    original_unknowns: tuple[str, ...]

    @model_validator(mode="after")
    def links(self):
        if (self.evidence_kind == "observed") != (self.owner_binding is not None):
            raise ValueError("Observed preparation requires complete owner binding")
        window_ids = tuple(w.window_id for w in self.windows)
        if len(set(window_ids)) != len(window_ids) or len(self.profiles) != len(self.windows):
            raise ValueError("Unique windows and one composite profile per window required")
        if {p.window_id for p in self.profiles} != set(window_ids):
            raise ValueError("Composite profiles must cover exactly the registered windows")
        for profile in self.profiles:
            if profile.binding_id != self.binding.binding_id or profile.owner_use_profile_sha256 != self.binding.use_profile_sha256:
                raise ValueError("Profile and owner binding mismatch")
        return self

    def window(self, window_id):
        for window in self.windows:
            if window.window_id == window_id:
                return window
        raise DomainError("M2_WINDOW_NOT_REGISTERED", "Select a registered immutable WindowPlan", 422)

    def profile(self, window_id):
        self.window(window_id)
        return next(p for p in self.profiles if p.window_id == window_id)


class Catalogue:
    def __init__(self, entries: tuple[PreparationEntry, ...] = ()):
        # Host-side construction for contract tests; no API, CLI or file import route.
        require(type(entries) is tuple and all(type(e) is PreparationEntry for e in entries),
                "M2_CATALOGUE_INVALID", "Typed immutable catalogue required")
        require(len({e.binding.binding_id for e in entries}) == len(entries),
                "M2_CATALOGUE_INVALID", "Duplicate data binding")
        self._entries = entries
        self.identity = digest(entries)

    @property
    def entries(self):
        return self._entries

    def get(self, binding_id):
        for entry in self._entries:
            if entry.binding.binding_id == binding_id:
                return entry
        raise DomainError("M2_OWNER_PROFILE_NOT_ADMITTED", "No registered M2 data use-profile for this binding", 422)


def check_context(entry: PreparationEntry, context: DecisionContext, *, run_id, request: BacktestRequest):
    require((context.run_id, context.request_sha256) == (run_id, digest(request)),
            "M2_CONTEXT_MISMATCH", "Context belongs to another run/request")
    require(context.window_id == request.window_id,
            "M2_WINDOW_CONTEXT_MISMATCH", "Context must use this request's selected window")
    require(request.data_binding_id == entry.binding.binding_id
            and request.assumptions.profile_sha256 == entry.profile(request.window_id).profile_sha256
            and not entry.unmet_requirements,
            "M2_REQUEST_BINDING_MISMATCH", "Request data/profile must match the selected catalogue entry")
    days = execution_days(entry, request)
    day = next((d for d in days if d.trading_date == context.trading_date), None)
    require(day is not None and day.previous_open == context.previous_open,
            "M2_WINDOW_CONTEXT_MISMATCH", "Use the owner-confirmed predecessor for this trading day")
    return day


def production_catalogue() -> Catalogue:
    return Catalogue()  # Fixed owner candidate exists; data review/native admission remain pending.
