"""Register/preflight only; no M2 worker or execution route is enabled."""

from typing import Literal

from ashare_quant.contracts import Contract

from .catalogue import production_catalogue
from .contracts import BacktestRequest, Sha, StrategyDefinition, StrategyVersion
from .preflight import PreflightReceipt, preflight, refuse_submission
from .registry import StrategyRegistry, template_source


class M2Capabilities(Contract):
    contract_version: Literal["m2.daily.v2"] = "m2.daily.v2"
    stage: Literal["bounded_native_candidate"] = "bounded_native_candidate"
    owner_candidate_version: Literal["0.7.0.dev1"] = "0.7.0.dev1"
    owner_product_review: Literal["PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"] = "PASS_CONDITIONAL_OWNER_API_AND_PUBLIC_CODEC"
    templates: tuple[str, ...] = ("buy_hold.v1", "previous_close_band.v1")
    registered_data_bindings: tuple[Sha, ...] = ()
    submission_enabled: Literal[False] = False
    native_ad08_validation: Literal["CANDIDATE_OBSERVED_NOT_ADMITTED"] = "CANDIDATE_OBSERVED_NOT_ADMITTED"
    real_multiday_validation: Literal["CANDIDATE_OBSERVED_NOT_ADMITTED"] = "CANDIDATE_OBSERVED_NOT_ADMITTED"
    paper_enabled: Literal[False] = False


Capabilities = M2Capabilities


class TemplateSource(Contract):
    version_id: Sha
    source_sha256: Sha
    source: str


def mount(app, store):
    registry = StrategyRegistry(store)
    catalogue = production_catalogue()

    @app.get("/api/v1/backtests/capabilities")
    def m2_capabilities() -> Capabilities:
        return Capabilities()

    @app.post("/api/v1/backtest-strategies", status_code=201)
    def m2_register(request: StrategyDefinition) -> StrategyVersion:
        return registry.register(request)

    @app.get("/api/v1/backtest-strategies")
    def m2_strategies() -> tuple[StrategyVersion, ...]:
        return registry.list()

    @app.get("/api/v1/backtest-strategies/{version_id}")
    def m2_strategy(version_id: str) -> StrategyVersion:
        return registry.get(version_id)

    @app.get("/api/v1/backtest-strategies/{version_id}/source")
    def m2_source(version_id: str) -> TemplateSource:
        version = registry.get(version_id)
        return TemplateSource(version_id=version.version_id, source_sha256=version.source_sha256, source=template_source())

    @app.post("/api/v1/backtests/preflight")
    def m2_preflight(request: BacktestRequest) -> PreflightReceipt:
        return preflight(request, registry, catalogue)

    @app.post("/api/v1/backtests")
    def m2_submit(request: BacktestRequest) -> PreflightReceipt:
        refuse_submission()
