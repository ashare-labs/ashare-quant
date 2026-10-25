"""Portfolio admission and immutable snapshots, sharing the original task controls."""

import json
import uuid

import tests.support.portfolio.publication as publication
from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.market.datasets import Datasets, check_frozen_data
from ashare_quant.runtime import runtime_fingerprint
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, require, sha
from ashare_quant.tasks.base import TaskService
from tests.support.portfolio.contracts import (
    PortfolioCatalogue,
    PortfolioIdentity,
    PortfolioJobList,
    PortfolioManifest,
    PortfolioSelection,
)
from tests.support.portfolio.executor import Executor
from tests.support.portfolio.registry import (
    ALGORITHMS,
    FEES,
    Registry,
    check_frozen_strategy,
    strategy_files,
)
from tests.support.portfolio.repository import Repository


class PortfolioJobs(TaskService):
    def executor_args(self, manifest):
        return (manifest.request.window_id,)

    def __init__(self, store, configuration=None):
        super().__init__(store, configuration)
        self.repo = Repository(store, self.repo.policy)
        self.executor = Executor(self.root, self.config, self.repo.policy)
        self.strategies = Registry(store)
        self.datasets = Datasets(self.root)

    def identity(self):
        return PortfolioIdentity(backend_version=__version__, source_sha256=self.source)

    def for_job(self, row):
        if json.loads(row["manifest"]).get("schema_version") == "portfolio-job-manifest.v1":
            return self
        from tests.support.daily.service import Jobs
        return Jobs(self.repo.store, self.config).for_job(row)

    def register_strategy(self, definition):
        self.guard()
        return self.strategies.register(definition)

    def import_data(self, path, name):
        self.guard()
        return self.datasets.import_file(path, name)

    def catalogue(self):
        self.guard()
        error = None
        try:
            self.executor.package()
        except DomainError as exc:
            error = exc.code
        return PortfolioCatalogue(
            service_identity=self.identity(),
            native_ready=error is None,
            native_error=error,
            datasets=self.datasets.list(),
            fee_models=({"id": "fixed-commission-tax.v1", **FEES},),
            strategy_algorithms=ALGORITHMS,
        )

    def service_status(self):
        from tests.support.portfolio.contracts import PortfolioServiceStatus

        status = super().service_status().model_dump()
        status["job_contract_version"] = "portfolio-jobs.v1"
        return PortfolioServiceStatus(**status)

    def preflight(self, selection):
        self.guard()
        return self.datasets.select(selection)[0]

    def create(self, request):
        self.guard()
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds):
            existing = self.repo.existing(request)
            if existing:
                return self.view(existing)
            self.repo.capacity()
            require(
                self.unaccepted_directories() < self.repo.policy.max_pending,
                "M2_JOB_PREPARATION_RESIDUE_LIMIT",
                "Inspect unaccepted preparations before creating more tasks",
            )
            selection = PortfolioSelection.model_validate(
                {k: getattr(request, k) for k in PortfolioSelection.model_fields}
            )
            report, document = self.datasets.select(selection)
            if not report.ready:
                raise DomainError(
                    "PORTFOLIO_DATA_GAPS",
                    "Requested scope cannot execute; inspect every reported field",
                    422,
                    details=report.model_dump(mode="json"),
                )
            require(
                request.assumption_ack_sha256 == report.assumption_ack_sha256,
                "PORTFOLIO_ASSUMPTIONS_CHANGED",
                "Acknowledge the preflight for this exact data, scope and mode",
            )
            self.executor.package()
            strategy = self.strategies.get(request.strategy_version_id)
            Registry.decode(canonical(strategy), request.strategy_version_id)
            minimum = (
                2
                if strategy.definition.algorithm == "weekly-reversal"
                else (
                    3
                    if strategy.definition.algorithm
                    in {"momentum", "low-volatility", "momentum-low-volatility"}
                    else 1
                )
            )
            if len(request.securities) < minimum:
                raise DomainError(
                    "PORTFOLIO_STRATEGY_UNIVERSE",
                    f"This algorithm requires at least {minimum} securities",
                    422,
                )
            job_id = "m2job-" + uuid.uuid4().hex
            root = self.directory(job_id)
            root.mkdir(mode=0o700)
            algorithm = strategy.definition.algorithm
            (root / algorithm).mkdir()
            files = {
                "submission.json": canonical(request),
                "input.json": canonical(document),
                **strategy_files(request, strategy, "bt-" + job_id.removeprefix("m2job-")),
            }
            for name, raw in files.items():
                atomic(root / name, raw)
            proof = self.executor.immediate("prepare", root, request.window_id, "prepare")
            session_raw = regular(root / "session.json")
            session = json.loads(session_raw)
            require(
                proof["cursor"] == 0
                and not proof["complete"]
                and proof["session_sha256"] == digest(session)
                and proof["session_file_sha256"] == sha(session_raw)
            )
            manifest = PortfolioManifest(
                job_id=job_id,
                request=request,
                service_identity=self.identity(),
                execution_scope=report.scope,
                strategy=strategy,
                data_identity=self.datasets.read(request.dataset_id)[0],
                preflight=report,
                session=session,
                session_sha256=digest(session),
                session_file_sha256=sha(session_raw),
                prepared_proof=proof,
                files={name: sha(raw) for name, raw in files.items()},
            )
            check_frozen_data(manifest, files["input.json"])
            atomic(root / "accepted-manifest.json", manifest)
            self.guard()
            return self.view(self.repo.insert(manifest))

    def check(self, row, *, execution=False):
        self.guard()
        try:
            manifest = self.repo.manifest(row)
            root = self.directory(row["job_id"])
            require(
                manifest.service_identity == self.identity(),
                "M2_JOB_SERVICE_CHANGED",
                "Use this task's original service package",
            )
            require(
                regular(root / "accepted-manifest.json") == canonical(manifest)
                and sha(regular(root / "session.json")) == manifest.session_file_sha256
                and regular(root / "session.json") == canonical(manifest.session)
                and digest(manifest.session) == manifest.session_sha256
            )
            captured = {name: regular(root / name) for name in manifest.files}
            for name, expected in manifest.files.items():
                require(
                    sha(captured[name]) == expected,
                    "PORTFOLIO_INPUT_CHANGED",
                    "An immutable task input changed",
                )
            require(
                json.loads(captured["submission.json"])
                == manifest.request.model_dump(mode="json")
            )
            check_frozen_strategy(manifest, captured)
            check_frozen_data(manifest, captured["input.json"])
            if execution:
                runtime_fingerprint(native=True)
                if row["checkpoint_sha256"] and row["status"] not in {"running", "cancelling"}:
                    require(
                        sha(regular(root / "native.chk")) == row["checkpoint_sha256"],
                        "M2_JOB_CHECKPOINT_CHANGED",
                        "Recorded checkpoint changed before resume",
                    )
            return manifest
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise DomainError(
                "PORTFOLIO_TASK_CONFLICT", "Frozen task identity is invalid", 409
            ) from exc

    def view(self, row):
        manifest = self.check(row)
        view = self.repo.view(row)
        if view.status == "succeeded":
            require(view.result is not None)
            publication.read(self.directory(view.job_id), manifest, view.result)
        else:
            require(view.result is None)
        self.guard()
        return view

    def list(self):
        return PortfolioJobList(
            items=tuple(
                self.view(row)
                for row in self.repo.rows()
                if json.loads(row["manifest"]).get("schema_version") == "portfolio-job-manifest.v1"
            )
        )

    def result(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row)
        require(
            row["status"] == "succeeded" and row["result"],
            "M2_JOB_RESULT_NOT_READY",
            "Task has no published result",
        )
        result = publication.read(self.directory(job_id), manifest, self.repo.view(row).result)
        self.guard()
        return result

    @staticmethod
    def publish(*args):
        return publication.publish(*args)
