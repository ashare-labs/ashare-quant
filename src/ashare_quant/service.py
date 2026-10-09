"""Application facade shared by CLI and HTTP. All real execution remains gated."""

import hashlib
from importlib.resources import files

from . import __version__
from .admission import native_runtime_identity
from .contracts import (
    DataAdmission,
    DomainError,
    Manifest,
    RunCreate,
    StrategyCreate,
    StrategyVersion,
)
from .data import ASHARE_COMMIT, ASHARE_TREE, ASHARE_VERSION, RQALPHA_COMMIT
from .engine import FIXTURE_IDENTITY, fixture_bytes, specifications
from .identity import source_inventory, source_sha256
from .rq_specs import RQ_IDENTITY, rq_specifications
from .runtime import runtime_fingerprint
from .store import Store, digest, now


class Service:
    def __init__(self, store: Store):
        self.store = store

    def register_strategy(self, definition: StrategyCreate) -> StrategyVersion:
        spec = (rq_specifications() if definition.engine_id == "rqalpha" else specifications())[definition.scenario]
        immutable = {"definition": definition.model_dump(mode="json"), "specification": spec}
        return self.store.put_strategy(StrategyVersion(
            version_id=digest(immutable), definition=definition, parameters=spec["parameters"],
            source_sha256=digest(spec), created_at=now(),
            execution_status="trusted_builtin" if definition.engine_id == "rqalpha" else "specification_only",
        ))

    def submit(self, request: RunCreate):
        existing = self.store.accepted_request(request)
        if existing is not None:
            return existing
        if (request.engine_id, request.mode) not in {("fixture-replay-v1", "fixture"), ("rqalpha", "synthetic")}:
            raise DomainError("ENGINE_NOT_ADMITTED",
                              "Only fixture replay or RQAlpha with synthetic data is enabled. "
                              "Real data research/paper await independent admission.", 422)
        strategy = self.store.strategy(request.strategy_version_id)
        if strategy.definition.engine_id != request.engine_id:
            raise DomainError("STRATEGY_ENGINE_MISMATCH", "Strategy version belongs to another engine", 422)
        native = request.engine_id == "rqalpha"
        spec = (rq_specifications() if native else specifications())[strategy.definition.scenario]
        if digest(spec) != strategy.source_sha256:
            raise DomainError("STRATEGY_SPEC_CHANGED", "Register a new immutable strategy version")
        trace_hash = digest(spec) if native else hashlib.sha256(fixture_bytes(strategy.definition.scenario)).hexdigest()
        manifest = Manifest(
            backend_version=__version__, backend_source_sha256=source_sha256(),
            request=request, strategy=strategy,
            engine=RQ_IDENTITY if native else FIXTURE_IDENTITY, fixture_sha256=trace_hash,
            fixture_spec_sha256=digest(spec),
            dependency_lock_sha256=files("ashare_quant").joinpath("lock.sha256").read_text().strip(),
            runtime_versions=runtime_fingerprint(),
            implementation_files=source_inventory(),
            native_runtime=native_runtime_identity() if native else {},
            candidate_data_version=ASHARE_VERSION, candidate_data_commit=ASHARE_COMMIT,
            candidate_data_tree=ASHARE_TREE, candidate_engine_commit=RQALPHA_COMMIT,
            data=DataAdmission(
                dataset_id=f"synthetic:{request.engine_id}:{strategy.definition.scenario}:v1",
                manifest_sha256=digest(spec), evidence_kind="synthetic", quality="unknown",
                quality_reference="manual synthetic specification; not an A数达 data attestation",
                point_in_time_verified=False, requirements_met=False,
                limitations=("Not real market data", "No A数达 admission", "No real paper admission"),
            ), created_at=now(),
            risk_profile="rqalpha-synthetic-no-live-orders" if native else "fixture-only-no-real-orders",
            execution_semantics="rqalpha_native_synthetic" if native else "scripted_projection_replay",
            real_strategy_executed=native,
        )
        return self.store.create_run(manifest)
