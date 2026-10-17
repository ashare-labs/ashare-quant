from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from ashare_quant.backtests.daily.execution_dates import ISODate
from ashare_quant.contracts import Contract

Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ResearchSecurity = Annotated[str, Field(pattern=r"^[0-9]{6}\.(XSHG|XSHE)$")]


class ResearchInputRef(Contract):
    kind: Literal["research", "baostock"]
    version_id: Sha


class DiagnosticQuery(Contract):
    source_alias: Alias
    security: ResearchSecurity
    trading_dates: tuple[ISODate, ...] = Field(min_length=1, max_length=31)
    price: ResearchInputRef | None = None
    status: ResearchInputRef | None = None
    preclose: ResearchInputRef | None = None
    require_complete: StrictBool = False

    @model_validator(mode="after")
    def finite_dates(self):
        if len(set(self.trading_dates)) != len(self.trading_dates):
            raise ValueError("Use unique trading date anchors")
        if (max(self.trading_dates) - min(self.trading_dates)).days > 30:
            raise ValueError("Date envelope may not exceed 31 natural days")
        return self


class OwnerError(Contract):
    origin: Literal["ashare-data"] = "ashare-data"
    code: str
    message: str
    details: dict | None = None


class OwnerRead(Contract):
    status: Literal["readable", "refused"]
    public_result: dict | None = None
    error: OwnerError | None = None

    @model_validator(mode="after")
    def outcome(self):
        if (self.status == "readable") != (self.public_result is not None and self.error is None):
            raise ValueError("Readable calls require a public result and no error")
        if self.status == "refused" and (self.public_result is not None or self.error is None):
            raise ValueError("Refused calls require the original owner error")
        return self


class OwnerIdentity(Contract):
    name: Literal["ashare-data"] = "ashare-data"
    version: str
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None
    wheel_sha256: Sha
    content_sha256: Sha
    projection_policy: Literal["source-calendar-1"] = "source-calendar-1"
    preflight_policy: Literal["research-input-preflight-1"] = "research-input-preflight-1"


class PreflightAvailability(Contract):
    status: Literal["available"] = "available"
    public_interface: Literal["Store.preflight_research_request"] = "Store.preflight_research_request"
    scope: Literal["single_security_research_inventory"] = "single_security_research_inventory"
    input_paths: Literal["configured_store_only"] = "configured_store_only"
    execution_permission: Literal[False] = False


class ExecutionAvailability(Contract):
    status: Literal["not_admitted"] = "not_admitted"
    initialization_read_domain: Literal["no_execution_owner_binding"] = "no_execution_owner_binding"
    execution_permission: Literal[False] = False
    requires_user_approval: Literal[False] = False
    reason: Literal["Calendar observations are not a bound owner window/profile/read_roles contract"] = "Calendar observations are not a bound owner window/profile/read_roles contract"


class TradingFacts(Contract):
    status: Literal["unknown"] = "unknown"
    security_eligibility: None = None
    source_suspension_and_st: Literal["preserved_in_public_research_preflight"] = "preserved_in_public_research_preflight"
    price_limits: None = None
    corporate_actions: None = None
    historical_pit_verified: Literal[False] = False


class DiagnosticResult(Contract):
    contract_version: Literal["source-diagnostics.v1"] = "source-diagnostics.v1"
    query: DiagnosticQuery
    query_sha256: Sha
    calendar_capture_id: Sha
    data_owner: OwnerIdentity
    backend_version: str
    backend_source_sha256: Sha
    calendar: OwnerRead
    calendar_links: OwnerRead
    research_readable: Literal["complete_research_values", "partial_research_values", "calendar_only", "blocked"]
    research_preflight: OwnerRead
    research_values_complete: StrictBool | None
    initialization: ExecutionAvailability = ExecutionAvailability()
    trading_facts: TradingFacts = TradingFacts()
    read_completed_at: AwareDatetime
    execution_owner_unchanged: Literal[True] = True
    network_used: Literal[False] = False
    limitations: tuple[str, ...] = (
        "Calendar is provider-wide; per-security source observations are preserved only in research_preflight.",
        "Complete research values do not establish historical eligibility, price limits, corporate actions, PIT or an execution owner plan.",
        "A readable calendar may still have unknown states or insufficient anchor boundaries; preserve its public report and links error.",
        "No collection, filling, source switching, execution-owner binding or job creation.",
    )


class DiagnosticCapabilities(Contract):
    contract_version: Literal["source-diagnostics.v1"] = "source-diagnostics.v1"
    configured_sources: tuple[Alias, ...]
    research_owner: OwnerIdentity
    research_preflight: PreflightAvailability = PreflightAvailability()
    initialization: ExecutionAvailability = ExecutionAvailability()
    trading_facts: TradingFacts = TradingFacts()
    local_configuration_only: Literal[True] = True
