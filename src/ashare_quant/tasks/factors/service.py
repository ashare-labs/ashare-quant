"""Freeze a bounded selection before admitting it to the existing worker queue."""

import json
import stat
import uuid
from functools import lru_cache

from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.factors.catalog import catalogue, detail
from ashare_quant.factors.compute import evaluation_available, validate_parameters
from ashare_quant.factors.inputs import Sources
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, require, sha
from ashare_quant.tasks.base import TaskService
from ashare_quant.tasks.contracts import ResultReference
from ashare_quant.tasks.factors.contracts import (
    FactorJobList,
    FactorManifest,
    FactorResult,
    FactorScope,
)
from ashare_quant.tasks.factors.executor import Executor
from ashare_quant.tasks.factors.repository import Repository
from ashare_quant.tasks.storage_budget import admit_storage, check_task_storage


def input_signature(path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise DomainError("M2_JOB_ARTIFACT_UNAVAILABLE", "Required local artifact is unavailable", 409) from exc
    require(stat.S_ISREG(info.st_mode), "M2_JOB_ARTIFACT_UNAVAILABLE",
            "Required local artifact is missing or linked")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


class FactorJobs(TaskService):
    active_queue = True

    def __init__(self, store, configuration=None, sources=None, product=None):
        super().__init__(store, configuration)
        self.repo = Repository(store, self.repo.policy, execution_source=self.source)
        self.executor = Executor(self.root, self.config, self.repo.policy)
        self.sources = Sources(sources, product)
        # Cache verification metadata, never the potentially 32 MiB input bytes.
        # Each service instance starts empty; execution always verifies afresh.
        self._verified_input = lru_cache(maxsize=1024)(self._verify_input)

    def for_job(self, row):
        if json.loads(row["manifest"]).get("schema_version") == "factor-job.v1":
            return self
        from ashare_quant.tasks.service import Jobs

        return Jobs(self.repo.store, self.config).for_job(row)

    def environment(self):
        self.guard()
        return digest(
            dict(
                source=self.source,
                sources=self.sources.identity(),
                control=str(self.repo.store.path),
            )
        )

    def catalogue(self, version):
        return dict(
            environment_id=self.environment(),
            items=catalogue(version),
            evaluation_available=evaluation_available(),
            limits=dict(max_securities=100, max_security_sessions=100000, max_input_bytes=33554432),
        )

    def create(self, request):
        self.guard()
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds):
            existing = self.repo.existing(request)
            if existing:
                return self.view(existing)
            require(
                request.environment_id == self.environment(),
                "FACTOR_ENVIRONMENT_CHANGED",
                "Environment changed; reload before explicitly starting a new request",
            )
            factor = detail(request.factor_id, request.definition)
            validate_parameters(request, factor)
            require(
                request.evaluation is None or evaluation_available(),
                "FACTOR_EVALUATION_UNAVAILABLE",
                "Evaluation module is not installed",
            )
            self.repo.capacity()
            require(
                self.unaccepted_directories() < self.repo.policy.max_pending,
                "FACTOR_STORAGE_LIMIT",
                "Inspect unfinished admission directories",
            )
            admit_storage(self)
            frozen = self.sources.freeze(request)
            self.executor.package()
            job_id = "m2job-" + uuid.uuid4().hex
            root = self.directory(job_id)
            root.mkdir(mode=0o700)
            session = dict(
                run_id=job_id.removeprefix("m2job-"),
                request_sha256=digest(request),
                input_sha256=digest(frozen),
                source_sha256=self.source,
            )
            files = {
                "submission.json": canonical(request),
                "input.json": canonical(frozen),
                "session.json": canonical(session),
            }
            for name, raw in files.items():
                atomic(root / name, raw)
            manifest = FactorManifest(
                job_id=job_id,
                request=request,
                source_sha256=self.source,
                service_identity={"source_sha256": self.source, "backend_version": __version__},
                execution_scope=FactorScope(
                    securities=request.securities, trading_dates=frozen["payload"]["sessions"]
                ),
                session=session,
                session_sha256=digest(session),
                session_file_sha256=sha(files["session.json"]),
                files={k: sha(v) for k, v in files.items()},
            )
            atomic(root / "accepted-manifest.json", manifest)
            check_task_storage(root, self.repo.policy)
            return self.view(self.repo.insert(manifest))

    @staticmethod
    def _verify_input(path, expected, signature):
        require(sha(regular(path)) == expected, "FACTOR_INPUT_CHANGED", "Frozen input changed")
        require(input_signature(path) == signature, "FACTOR_INPUT_CHANGED",
                "Frozen input changed during verification")

    def check(self, row, *, execution=False):
        self.guard()
        manifest = self.repo.manifest(row)
        if execution:
            require(
                manifest.source_sha256 == self.source,
                "M2_JOB_SERVICE_CHANGED",
                "Use this result's original service package",
            )
        root = self.directory(row["job_id"])
        require(regular(root / "accepted-manifest.json") == canonical(manifest))
        verify = (self._verified_input if not execution and row["status"] in
                  {"succeeded", "failed", "cancelled"} else self._verify_input)
        for name, expected in manifest.files.items():
            path = root / name
            signature = input_signature(path)
            verify(path, expected, signature)
            require(input_signature(path) == signature, "FACTOR_INPUT_CHANGED",
                    "Frozen input changed during verification")
        if execution:
            require(
                row["attempt_count"] == 0,
                "FACTOR_RESTART_REQUIRED",
                "Interrupted computation requires an explicit new run; saved results remain readable",
            )
        return manifest

    def view(self, row):
        self.check(row)
        return self.repo.view(row)

    def list(self):
        return FactorJobList(
            items=tuple(
                self.view(r)
                for r in self.repo.rows()
                if json.loads(r["manifest"]).get("schema_version") == "factor-job.v1"
            )
        )

    def resume(self, job_id):
        raise DomainError(
            "FACTOR_RESTART_REQUIRED",
            "Create a new run explicitly; completed results restore without computation",
            409,
        )

    def result(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row)
        require(
            row["status"] == "succeeded" and row["result"],
            "M2_JOB_RESULT_NOT_READY",
            "No completed result",
        )
        reference = ResultReference.model_validate_json(row["result"])
        require(reference.result_id == job_id and reference.evidence_manifest_sha256 == digest(manifest),
                "FACTOR_RESULT_CHANGED", "Published result belongs to another task")
        raw = regular(self.directory(job_id) / "published.json")
        require(
            sha(raw) == reference.snapshot_sha256,
            "FACTOR_RESULT_CHANGED",
            "Published result changed",
        )
        value = FactorResult.model_validate_json(raw)
        require(value.job_id == job_id, "FACTOR_RESULT_CHANGED")
        return value

    def publish(self, root, manifest, invocation, attempt_count, logs):
        raw = regular(root / "result.json")
        value = FactorResult.model_validate_json(raw)
        proof = json.loads(regular(root / (invocation + ".proof.json")))
        require(
            proof["complete"]
            and proof["receipt_sha256"] == digest(json.loads(raw))
            and value.request == manifest.request
            and value.job_id == manifest.job_id
            and value.provenance["input_sha256"] == manifest.session["input_sha256"],
            "FACTOR_RESULT_CHANGED",
            "Result differs from its frozen request/input",
        )
        raw = canonical(value)
        check_task_storage(root, self.repo.policy, additional_bytes=len(raw))
        atomic(root / "published.json", raw)
        return ResultReference(
            result_id=manifest.job_id,
            href=f"/api/v1/factors/jobs/{manifest.job_id}/result",
            snapshot_sha256=sha(canonical(value)),
            evidence_manifest_sha256=digest(manifest),
            publication=f"publication-{attempt_count}",
        )
