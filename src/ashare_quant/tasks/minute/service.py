"""Freeze trusted source and a release before queuing automatic data preparation."""

import ast
import json
import uuid

from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, require, sha
from ashare_quant.tasks.base import TaskService
from ashare_quant.tasks.contracts import ResultReference
from ashare_quant.tasks.minute.contracts import (
    PythonJobList,
    PythonManifest,
    PythonResult,
    PythonScope,
)
from ashare_quant.tasks.minute.executor import Executor
from ashare_quant.tasks.minute.repository import Repository
from ashare_quant.tasks.storage_budget import admit_storage, check_task_storage


class PythonJobs(TaskService):
    active_queue = True

    def __init__(self, store, configuration=None, product=None):
        from ashare_quant.providers.daily.history import configured_factors

        super().__init__(store, configuration)
        self.repo = Repository(store, self.repo.policy, execution_source=self.source)
        self.executor = Executor(self.root, self.config, self.repo.policy)
        self.product = product
        self.history_factors = configured_factors()

    def for_job(self, row):
        if json.loads(row["manifest"]).get("schema_version") in {"python-minute-job.v1", "python-demand-job.v2"}:
            return self
        from ashare_quant.tasks.service import Jobs

        return Jobs(self.repo.store, self.config).for_job(row)

    def environment(self):
        self.guard()
        require(
            self.product is not None,
            "PYTHON_DATA_NOT_CONFIGURED",
            "Configure the formal product profile",
        )
        config = self.product.configured()
        return digest(
            {
                "source": self.source,
                "configuration": config.document,
                "data_root": str(config.data_root),
                "control": str(self.repo.store.path),
                "history_factors": self.history_factors,
                "resource_policy_sha256": digest(self.repo.policy),
            }
        )

    def create(self, request):
        self.guard()
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds):
            existing = self.repo.existing(request)
            if existing:
                return self.view(existing)
            require(
                request.environment_id == self.environment(),
                "PYTHON_ENVIRONMENT_CHANGED",
                "Service or data configuration changed. Reload and explicitly submit in the new environment",
            )
            submitted_strategy_id = request.strategy_id
            if request.strategy_id is not None:
                from ashare_quant.strategies.files import StrategyFiles

                # The client explicitly supplies the saved file identity. Unsaved edits
                # may differ; this task freezes its submitted source independently.
                saved = StrategyFiles(self.product.configured().control_db.parent).read(request.strategy_id)
                request = request.model_copy(update={"strategy_id": saved["strategy_id"]})
            self.repo.capacity()
            require(
                self.unaccepted_directories() < self.repo.policy.max_pending,
                "PYTHON_PREPARATION_RESIDUE_LIMIT",
                "Inspect unaccepted task directories before submitting more",
            )
            admit_storage(self)
            require(
                len(request.source.encode()) <= 262144
                and len(canonical(request.parameters)) <= 16384,
                "PYTHON_SOURCE_SIZE",
                "Source must fit 256 KiB and parameters 16 KiB",
            )
            try:
                tree = ast.parse(request.source, filename="strategy.py")
                from ashare_quant.strategies.imports import MIGRATION_MESSAGE, legacy_nodes

                if list(legacy_nodes(tree)):
                    raise DomainError("AQUANT_LEGACY_IMPORT", MIGRATION_MESSAGE, 422)
            except SyntaxError as exc:
                raise DomainError(
                    "PYTHON_SYNTAX_ERROR",
                    f"Line {exc.lineno}: {exc.msg}",
                    422,
                    details={"line": exc.lineno, "column": exc.offset},
                ) from exc
            require(
                self.product is not None,
                "PYTHON_DATA_NOT_CONFIGURED",
                "Configure a formal market release",
            )

            def pin_release(owner):
                release = getattr(owner, "release", None)
                require(
                    release is not None,
                    "PYTHON_RELEASE_REQUIRED",
                    "Select an immutable formal market release",
                )
                return dict(
                    root=str(release.root),
                    release_id=release.release_id,
                    manifest_sha256=release.sha256,
                )

            pin = self.product.call(pin_release)
            from ashare_quant.tasks.minute.benchmark import configured_release
            benchmark = configured_release(self.product.configured())
            from ashare_quant.tasks.minute.demand import run_scope

            days, _ = run_scope(pin, str(request.start_date), str(request.end_date))
            self.executor.package()
            job_id = "m2job-" + uuid.uuid4().hex
            root = self.directory(job_id)
            root.mkdir(mode=0o700)
            from ashare_quant.providers.daily.history import freeze_factors
            from ashare_quant.providers.runtime import pin as owner_pin
            from ashare_quant.runtime import runtime_fingerprint

            session = {
                "runtime": runtime_fingerprint(native=True),
                "owner_pin_sha256": digest(owner_pin()),
                "run_id": job_id.removeprefix("m2job-"),
                "release": pin,
                "request_sha256": digest(request),
                "execution": "fresh_process_no_python_resume",
                "data_access": "demand.v2",
                "data_budget": self.repo.policy.model_dump(mode="json"),
                "resource_policy": self.repo.policy.model_dump(mode="json"),
                "history_factors": freeze_factors(self.history_factors, root),
            }
            if benchmark is not None:
                session["benchmark_release"] = benchmark
            if submitted_strategy_id != request.strategy_id:
                session["submitted_strategy_id"] = submitted_strategy_id
            files = {
                "submission.json": canonical(request),
                "strategy.py": request.source.encode(),
                "session.json": canonical(session),
            }
            for name, raw in files.items():
                atomic(root / name, raw)
            manifest = PythonManifest(
                job_id=job_id,
                request=request,
                source_sha256=self.source,
                service_identity={"source_sha256": self.source, "backend_version": __version__},
                execution_scope=PythonScope(
                    securities=request.securities, trading_dates=days, frequency=request.frequency
                ),
                release=pin,
                session=session,
                session_sha256=digest(session),
                session_file_sha256=sha(files["session.json"]),
                files={name: sha(raw) for name, raw in files.items()},
            )
            atomic(root / "accepted-manifest.json", manifest)
            check_task_storage(root, self.repo.policy)
            return self.view(self.repo.insert(manifest))

    def check(self, row, *, execution=False):
        self.guard()
        manifest = self.repo.manifest(row)
        if execution:
            require(
                manifest.source_sha256 == self.source,
                "M2_JOB_SERVICE_CHANGED",
                "Use the task's original service package",
            )
        root = self.directory(row["job_id"])
        require(regular(root / "accepted-manifest.json") == canonical(manifest))
        for name, expected in manifest.files.items():
            require(
                sha(regular(root / name)) == expected,
                "PYTHON_INPUT_CHANGED",
                "Frozen source or configuration changed",
            )
        # Arbitrary Python has no checkpoint/resume contract. A lost worker must
        # never silently replay an already started strategy.
        if execution and row["status"] in {"queued", "running", "cancelling"}:
            require(
                row["attempt_count"] == 0,
                "PYTHON_RESTART_REQUIRED",
                "Interrupted Python state cannot resume. Create a new isolated run explicitly",
            )
        return manifest

    def view(self, row):
        self.check(row)
        view = self.repo.view(row)
        phase = view.display_status or view.status
        path = self.directory(view.job_id) / "phase.json"
        if phase == "running" and path.exists():
            phase = json.loads(regular(path)).get("phase", "running")
        return view.model_copy(update={"phase": phase})

    def list(self):
        return PythonJobList(
            items=tuple(
                self.view(r)
                for r in self.repo.rows()
                if json.loads(r["manifest"]).get("schema_version") in {"python-minute-job.v1", "python-demand-job.v2"}
            )
        )

    def resume(self, job_id):
        self.check(self.repo.row(job_id))
        raise DomainError(
            "PYTHON_RESTART_REQUIRED",
            "Create a new isolated run; Python memory is not resumable",
            409,
        )

    def result(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row)
        require(
            row["status"] == "succeeded" and row["result"],
            "M2_JOB_RESULT_NOT_READY",
            "Run has no completed result",
        )
        reference = ResultReference.model_validate_json(row["result"])
        require(reference.result_id == job_id and reference.evidence_manifest_sha256 == digest(manifest),
                "PYTHON_RESULT_CHANGED", "Published result belongs to another task")
        raw = regular(self.directory(job_id) / "published.json")
        require(
            sha(raw) == reference.snapshot_sha256,
            "PYTHON_RESULT_CHANGED",
            "Completed result changed",
        )
        value = PythonResult.model_validate_json(raw)
        require(value.job_id == job_id, "PYTHON_RESULT_CHANGED")
        return value

    def publish(self, root, manifest, invocation, attempt_count, logs):
        native = json.loads(regular(root / "result.json"))
        proof = json.loads(regular(root / (invocation + ".proof.json")))
        require(
            proof["complete"] and proof["receipt_sha256"] == digest(native), "PYTHON_RESULT_CHANGED"
        )
        preparation = json.loads(regular(root / "preparation.json"))
        require(
            preparation["input_sha256"] == digest(json.loads(regular(root / "input.json"))),
            "PYTHON_INPUT_CHANGED",
        )
        if manifest.schema_version == "python-demand-job.v2":
            require(native.get("data_input_sha256") == preparation["input_sha256"],
                    "PYTHON_INPUT_CHANGED", "Completed result does not bind its actual data reads")
        events = []
        for line in regular(root / "events.jsonl").decode().splitlines():
            event = json.loads(line)
            if event.get("event") in {"log", "record"} or event.get("kind") in {"log", "record"}:
                events.append(event)
        values = [manifest.request.initial_cash] + [r["total_value"] for r in native["accounts"]]
        peak, drawdown = values[0], 0.0
        for value in values:
            peak = max(peak, value)
            drawdown = max(drawdown, (peak - value) / peak)
        value = PythonResult(
            job_id=manifest.job_id,
            name=manifest.request.name,
            release=manifest.release,
            parameters=manifest.request.parameters,
            preparation=preparation,
            native=native,
            logs=tuple(events[-500:]),
            summary={
                "initial_cash": values[0],
                "final_equity": native["final_account"]["total_value"],
                "net_return": native["total_return"],
                "net_change": native["total_pnl"],
                "max_drawdown": drawdown,
                "drawdown_sampling": "end_of_day",
                "trade_count": len(native["trades"]),
                "order_count": len(native["orders"]),
            },
        )
        raw = canonical(value)
        check_task_storage(root, self.repo.policy, additional_bytes=len(raw),
                           host_policy=getattr(self, "host_policy", None))
        atomic(root / "published.json", raw)
        return ResultReference(
            result_id=manifest.job_id,
            href=f"/api/v1/python/jobs/{manifest.job_id}/result",
            snapshot_sha256=sha(canonical(value)),
            evidence_manifest_sha256=digest(manifest),
            publication=f"publication-{attempt_count}",
        )
