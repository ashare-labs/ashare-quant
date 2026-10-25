"""Finite research control service; reads never launch the native engine."""

import json
import uuid

import tests.support.daily.publication as publication
from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.runtime import runtime_fingerprint
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, require, sha
from ashare_quant.tasks.base import TaskService
from ashare_quant.tasks.release import EXECUTION_SOURCE_SHA256, EXECUTION_VERSION
from tests.support.daily.catalogue import window
from tests.support.daily.contracts import (
    JobCatalogue,
    JobList,
    JobManifest,
    JobServiceIdentity,
)
from tests.support.daily.executor import Configuration, Executor, runner_sha
from tests.support.legacy_backtests.execution_dates import resolve
from tests.support.legacy_backtests.parameter_policy import (
    RegisteredPythonParameters,
    RegisteredRuleParameters,
    check_strategy,
)
from tests.support.legacy_provider.owner_wire import owner_call, verify_runtime
from tests.support.legacy_provider.portable import LocalDataBinding, bind_dataset
from tests.support.legacy_python.registry import register as register_python
from tests.support.legacy_python.registry import source_bytes
from tests.support.legacy_python.spec import PythonCapabilities, is_python, task_source
from tests.support.legacy_strategies.registry import StrategyRegistry


class Jobs(TaskService):
    def executor_args(self, manifest):
        return (manifest.request.window_id,)

    def __init__(self, store, configuration=None):
        super().__init__(store, configuration or Configuration.environment())
        from tests.support.daily.contracts import JobView
        self.repo.view_type = JobView
        self.repo.manifest = legacy_manifest
        self.strategies = StrategyRegistry(store)
        self.executor = Executor(self.root, self.config, self.repo.policy)

    def identity(self):
        return JobServiceIdentity(backend_version=__version__, source_sha256=self.source)

    def for_job(self, row):
        if json.loads(row["manifest"]).get("schema_version") == "factor-job.v1":
            from ashare_quant.tasks.factors.service import FactorJobs
            return FactorJobs(self.repo.store, self.config, sources={})
        try:
            portfolio = json.loads(row["manifest"]).get("schema_version") == "portfolio-job-manifest.v1"
        except (ValueError, TypeError, AttributeError) as exc:
            raise DomainError("M2_JOB_IDENTITY_CONFLICT", "Stored task identity is malformed", 409) from exc
        if json.loads(row["manifest"]).get("schema_version") == "python-minute-job.v1":
            from ashare_quant.tasks.minute.service import PythonJobs
            return PythonJobs(self.repo.store, self.config)
        if portfolio:
            from tests.support.portfolio.service import PortfolioJobs

            return PortfolioJobs(self.repo.store, self.config)
        return self

    @staticmethod
    def publish(*args):
        return publication.publish(*args)


    def register_strategy(self, definition):
        self.guard()
        value = self.strategies.register(definition)
        self.guard()
        return value

    def list_strategies(self):
        self.guard()
        return tuple(
            v for v in self.strategies.list() if v.definition.template == "decision_rules.v1"
        )

    def get_strategy(self, version_id):
        self.guard()
        value = self.strategies.get(version_id)
        require(
            value.definition.template == "decision_rules.v1",
            "M2_RULE_STRATEGY_REQUIRED",
            "This endpoint returns user rule strategies",
        )
        return value

    def register_python(self, path, descriptor):
        self.guard()
        value = register_python(self.repo.store, path, descriptor)
        self.guard()
        return value

    def python_strategies(self):
        self.guard()
        return tuple(v for v in self.strategies.list() if is_python(v))

    def python_strategy(self, version_id):
        self.guard()
        value = self.strategies.get(version_id)
        require(
            is_python(value), "PYTHON_STRATEGY_REQUIRED", "Use a locally registered Python version"
        )
        return value

    def binding(self, root=None):
        path = (root or self.root) / "data-binding.json"
        require(path.exists(), "M2_JOB_DATA_NOT_BOUND", "Import and bind an owner dataset first")
        try:
            raw = regular(path)
            binding = LocalDataBinding.model_validate_json(raw).checked()
            require(
                raw == canonical(binding), "M2_DATA_BINDING_CHANGED", "Frozen binding bytes differ"
            )
            return binding
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise DomainError(
                "M2_DATA_BINDING_CHANGED", "Local owner binding is malformed or unavailable", 409
            ) from exc

    def bind(self, dataset_id):
        from ashare_data import Store as DataStore

        self.guard()
        self.config.check()
        binding = bind_dataset(DataStore(self.config.owner_store), dataset_id)
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds):
            self.guard()
            atomic(self.root / "data-binding.json", binding)
        return self.catalogue()

    def import_data(self, directory):
        from ashare_data import Store as DataStore

        require(
            self.config.owner_store is not None,
            "M2_JOB_NOT_CONFIGURED",
            "Set ASHARE_QUANT_DAILY_STORE before importing",
        )
        self.guard()
        require(
            not self.config.owner_store.is_symlink(),
            "M2_JOB_DATA_UNAVAILABLE",
            "Owner Store cannot be a symlink",
        )

        def import_owner():
            verify_runtime()
            store = (
                DataStore(self.config.owner_store)
                if self.config.owner_store.exists()
                else DataStore.init(self.config.owner_store)
            )
            return store.import_portable(directory)

        dataset_id = owner_call(import_owner)
        return self.bind(dataset_id)

    def catalogue(self):
        self.guard()
        windows, binding, error, native_error = (), None, None, None
        try:
            self.config.check()
            binding = self.binding()
            windows = tuple(
                window(binding, e.owner_binding.owner_window_id) for e in binding.entries
            )
        except DomainError as exc:
            error = exc.code
        try:
            runtime_fingerprint(native=True)
        except DomainError as exc:
            native_error = exc.code
        self.guard()
        return JobCatalogue(
            python_strategies=PythonCapabilities(
                attempt_seconds=getattr(self.repo.policy, "execution_seconds", 120),
                output_bytes=getattr(self.repo.policy, "output_bytes", 1048576),
            ),
            configured=error is None,
            configuration_error=error,
            data_binding_sha256=binding.identity if binding else None,
            native_ready=native_error is None,
            native_error=native_error,
            windows=windows,
            service_identity=self.identity(),
            executor_version=EXECUTION_VERSION,
            executor_source_sha256=EXECUTION_SOURCE_SHA256,
        )


    def check(self, row, *, execution=False):
        self.guard()
        manifest = self.repo.manifest(row)
        root = self.directory(row["job_id"])
        require(
            manifest.service_identity == self.identity(),
            "M2_JOB_SERVICE_CHANGED",
            "Task requires its original service package",
        )
        require(
            manifest.executor_source_sha256 == EXECUTION_SOURCE_SHA256
            and manifest.runner_sha256 == runner_sha()
            and manifest.parameters_sha256 == digest(manifest.request.parameters)
            and regular(root / "parameters.json") == canonical(manifest.request.parameters)
            and regular(root / "strategy.json") == canonical(manifest.strategy)
            and manifest.session["strategy"] == manifest.strategy.model_dump(mode="json")
            and manifest.job_directory == row["job_id"]
            and manifest.session_sha256 == digest(manifest.session)
            and regular(root / "accepted-manifest.json") == canonical(manifest)
            and sha(regular(root / "session.json")) == manifest.session_file_sha256
        )
        check_strategy(
            manifest.request.parameters,
            StrategyRegistry._decode(canonical(manifest.strategy), manifest.strategy.version_id),
        )
        task_source(root, manifest.strategy)
        require(
            manifest.python_source_sha256
            == (manifest.strategy.source_sha256 if is_python(manifest.strategy) else None)
        )
        require(
            manifest.session["strategy_parameters"]
            == getattr(manifest.request.parameters, "strategy_parameters", {})
        )
        require(manifest.session["data_binding_sha256"] == manifest.data_binding_sha256)
        binding = self.binding(root)
        approved = window(binding, manifest.request.window_id)
        scope = resolve(binding.entry(manifest.request.window_id), manifest.request.end_date)
        require(
            manifest.execution_scope == scope
            and regular(root / "execution-scope.json") == canonical(scope)
            and manifest.session["request"]["end_date"]
            == (manifest.request.end_date.isoformat() if manifest.request.end_date else None),
            "M2_EXECUTION_SCOPE_CHANGED",
            "Frozen end date or execution prefix differs",
        )
        require(
            binding.identity == manifest.data_binding_sha256 == manifest.request.data_binding_sha256
            and manifest.session["entry"]
            == binding.entry(manifest.request.window_id).model_dump(mode="json")
            and approved == manifest.approved_window
            and manifest.request.assumption_ack_sha256 == approved.assumption_ack_sha256
        )
        if execution:
            self.config.check()
            if row["checkpoint_sha256"] and row["status"] not in {"running", "cancelling"}:
                require(
                    sha(regular(root / "native.chk")) == row["checkpoint_sha256"],
                    "M2_JOB_CHECKPOINT_CHANGED",
                    "Durably recorded checkpoint changed before resume",
                )
        return manifest



    def create(self, request):
        self.guard()
        # Creation is infrequent and bounded. Serializing preparation avoids two
        # sessions/accounts for concurrent duplicate idempotency keys.
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds):
            existing = self.repo.existing(request)
            if existing:
                return self.view(existing)
            self.repo.capacity()
            if self.unaccepted_directories() >= self.repo.policy.max_pending:
                raise DomainError("M2_JOB_PREPARATION_RESIDUE_LIMIT",
                                  "Unaccepted preparation directories reached the budget; preserve and inspect them before creating more tasks", 503)
            self.config.check()
            binding = self.binding()
            require(
                request.data_binding_sha256 == binding.identity,
                "M2_DATA_BINDING_CHANGED",
                "Acknowledge the active local data binding from the catalogue",
            )
            approved = window(binding, request.window_id)
            scope = resolve(binding.entry(request.window_id), request.end_date)
            require(
                request.assumption_ack_sha256 == approved.assumption_ack_sha256,
                "M2_JOB_ASSUMPTIONS_REQUIRED",
                "Acknowledge the exact seven conditional assumptions for this window",
            )
            # Refuse unavailable native dependencies before strategy/task artifacts exist.
            # Executor.command retains its own check immediately before subprocess launch.
            self.executor.package()
            strategy = (
                (
                    self.python_strategy(request.parameters.strategy_version_id)
                    if isinstance(request.parameters, RegisteredPythonParameters)
                    else self.get_strategy(request.parameters.strategy_version_id)
                )
                if isinstance(request.parameters, RegisteredRuleParameters)
                else self.strategies.register(request.parameters.definition())
            )
            check_strategy(request.parameters, strategy)
            job_id = "m2job-" + uuid.uuid4().hex
            root = self.directory(job_id)
            root.mkdir(mode=0o700)
            atomic(root / "submission.json", request)
            atomic(root / "parameters.json", request.parameters)
            atomic(root / "strategy.json", strategy)
            if is_python(strategy):
                atomic(root / "strategy-source.py", source_bytes(self.repo.store, strategy))
            atomic(root / "data-binding.json", binding)
            atomic(root / "execution-scope.json", scope)
            proof = self.executor.immediate("prepare", root, request.window_id, "prepare")
            session_raw = regular(root / "session.json")
            session = json.loads(session_raw)
            require(
                proof["data_binding_sha256"] == binding.identity
                and proof["execution_scope_sha256"] == digest(scope)
                and session["request"]["end_date"]
                == (request.end_date.isoformat() if request.end_date else None)
                and proof["cursor"] == 0
                and not proof["complete"]
                and proof["session_sha256"] == digest(session)
                and proof["session_file_sha256"] == sha(session_raw)
                and proof["parameters_sha256"] == digest(request.parameters)
                and session["request"]["initial_cash"] == request.parameters.initial_cash
                and session["strategy"] == strategy.model_dump(mode="json")
                and proof["strategy_sha256"] == digest(strategy)
                and session["entry"]["binding"]["dataset_id"] == approved.data_identity.dataset_id
                and session["request"]["assumptions"]["profile_sha256"]
                == approved.assumption_ack_sha256
            )
            self.guard()
            manifest = JobManifest(
                execution_scope=scope,
                job_id=job_id,
                request=request,
                service_identity=self.identity(),
                executor_source_sha256=EXECUTION_SOURCE_SHA256,
                runner_sha256=runner_sha(),
                parameters_sha256=digest(request.parameters),
                data_binding_sha256=binding.identity,
                job_directory=job_id,
                session_sha256=digest(session),
                session_file_sha256=sha(session_raw),
                session=session,
                strategy=strategy,
                python_source_sha256=strategy.source_sha256 if is_python(strategy) else None,
                approved_window=approved,
                prepared_proof=proof,
            )
            atomic(root / "accepted-manifest.json", manifest)
            return self.view(self.repo.insert(manifest))

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
        return JobList(items=tuple(self.view(row) for row in self.repo.rows()
            if json.loads(row["manifest"]).get("schema_version") not in {"portfolio-job-manifest.v1", "python-minute-job.v1", "factor-job.v1"}))


    def result(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row)
        require(
            row["status"] == "succeeded" and row["result"],
            "M2_JOB_RESULT_NOT_READY",
            "Only a durably published successful task has a result",
        )
        view = self.repo.view(row)
        value = publication.read(self.directory(job_id), manifest, view.result)
        self.guard()
        return value


def legacy_manifest(row):
    from tests.support.daily.contracts import JobCreate, JobManifest
    value = JobManifest.model_validate_json(row["manifest"])
    require(digest(value) == row["manifest_sha256"] and value.job_id == row["job_id"]
            and digest(value.request) == row["request_hash"]
            and value.request == JobCreate.model_validate_json(row["request"])
            and row["account_id"] == "bt-" + value.session["run_id"])
    return value
