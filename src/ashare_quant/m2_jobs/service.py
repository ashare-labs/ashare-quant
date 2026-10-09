"""Finite research control service; reads never launch the native engine."""

import json
import uuid

from pydantic import TypeAdapter

from ashare_quant import __version__
from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_sha256
from ashare_quant.m2.registry import source_sha256 as template_sha256
from ashare_quant.m2.registry import version_identity
from ashare_quant.m2_results.service import (
    RESULT_IDS,
    result_detail,
)

from . import publication
from .contracts import (
    AllowedWindow,
    JobCatalogue,
    JobID,
    JobList,
    JobLogs,
    JobManifest,
    JobServiceIdentity,
)
from .executor import Configuration, Executor, runner_sha, wheel_bytes
from .files import atomic, canonical, digest, lock, regular, require, sha
from .release import EXECUTION_SOURCE_SHA256, EXECUTION_WHEEL_SHA256
from .repository import Repository


class Jobs:
    def __init__(self, store, configuration=None):
        self.repo = Repository(store)
        self.config = configuration or Configuration.environment()
        self.root = store.path.with_name(store.path.name + ".m2-jobs")
        require(not self.root.is_symlink())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.source = source_sha256()
        self.executor = Executor(self.root, self.config)

    def identity(self):
        return JobServiceIdentity(backend_version=__version__, source_sha256=self.source)

    def guard(self):
        require(
            source_sha256() == self.source,
            "M2_JOB_SERVICE_CHANGED",
            "Service changed after startup; use the frozen task version",
        )
        wheel_bytes()

    def catalogue(self):
        self.guard()
        windows = []
        for window, rid in RESULT_IDS.items():
            result = result_detail(rid)
            windows.append(
                AllowedWindow(
                    scope=result.scope,
                    reviewed_example_strategy_version_id=result.strategy.version_id,
                    assumption_ack_sha256=result.assumption_bundle_sha256,
                    assumptions=result.assumptions,
                    data_identity=result.data_identity,
                    reviewed_example_id=rid,
                )
            )
        error = None
        try:
            self.config.check()
        except DomainError as exc:
            error = exc.code
        self.guard()
        return JobCatalogue(
            configured=error is None,
            configuration_error=error,
            windows=tuple(windows),
            service_identity=self.identity(),
            executor_wheel_sha256=EXECUTION_WHEEL_SHA256,
        )

    def directory(self, job_id):
        job_id = TypeAdapter(JobID).validate_python(job_id)
        require(not self.root.is_symlink())
        path = self.root / job_id
        require(not path.is_symlink())
        return path

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
            manifest.executor_wheel_sha256 == EXECUTION_WHEEL_SHA256
            and manifest.executor_source_sha256 == EXECUTION_SOURCE_SHA256
            and manifest.runner_sha256 == runner_sha()
            and manifest.parameters_sha256 == digest(manifest.request.parameters)
            and regular(root / "parameters.json") == canonical(manifest.request.parameters)
            and manifest.job_directory == str(root)
            and manifest.session_sha256 == digest(manifest.session)
            and regular(root / "accepted-manifest.json") == canonical(manifest)
            and sha(regular(root / "session.json")) == manifest.session_file_sha256
        )
        approved = next(
            w for w in self.catalogue().windows if w.scope.window_id == manifest.request.window_id
        )
        require(
            approved == manifest.approved_window
            and manifest.request.assumption_ack_sha256 == approved.assumption_ack_sha256
        )
        if execution:
            self.config.check()
            require(
                manifest.owner_store == str(self.config.owner_store),
                "M2_JOB_DATA_CONFIG_CHANGED",
                "Resume with the original owner store",
            )
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
        with lock(self.root / "creation.lock", blocking=True):
            existing = self.repo.existing(request)
            if existing:
                return self.view(existing)
            self.config.check()
            approved = next(
                w for w in self.catalogue().windows if w.scope.window_id == request.window_id
            )
            require(
                request.assumption_ack_sha256 == approved.assumption_ack_sha256,
                "M2_JOB_ASSUMPTIONS_REQUIRED",
                "Acknowledge the exact seven conditional assumptions for this window",
            )
            job_id = "m2job-" + uuid.uuid4().hex
            root = self.directory(job_id)
            root.mkdir(mode=0o700)
            atomic(root / "parameters.json", request.parameters)
            proof = self.executor.immediate("prepare", root, request.window_id, "prepare")
            session_raw = regular(root / "session.json")
            session = json.loads(session_raw)
            require(
                proof["cursor"] == 0
                and not proof["complete"]
                and proof["session_sha256"] == digest(session)
                and proof["session_file_sha256"] == sha(session_raw)
                and proof["parameters_sha256"] == digest(request.parameters)
                and session["request"]["initial_cash"] == request.parameters.initial_cash
                and session["strategy"]["definition"]
                == request.parameters.definition().model_dump(mode="json")
                and session["strategy"]["version_id"]
                == version_identity(request.parameters.definition(), template_sha256())
                and session["entry"]["binding"]["dataset_id"] == approved.data_identity.dataset_id
                and session["request"]["assumptions"]["profile_sha256"]
                == approved.assumption_ack_sha256
            )
            self.guard()
            manifest = JobManifest(
                job_id=job_id,
                request=request,
                service_identity=self.identity(),
                executor_wheel_sha256=EXECUTION_WHEEL_SHA256,
                executor_source_sha256=EXECUTION_SOURCE_SHA256,
                runner_sha256=runner_sha(),
                parameters_sha256=digest(request.parameters),
                owner_store=str(self.config.owner_store),
                job_directory=str(root),
                session_sha256=digest(session),
                session_file_sha256=sha(session_raw),
                session=session,
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

    def get(self, job_id):
        return self.view(self.repo.row(job_id))

    def list(self):
        return JobList(items=tuple(self.view(row) for row in self.repo.rows()))

    def logs(self, job_id):
        self.check(self.repo.row(job_id))
        return JobLogs(job_id=job_id, items=tuple(self.repo.logs(job_id)))

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

    def cancel(self, job_id):
        self.check(self.repo.row(job_id))
        return self.view(self.repo.cancel(job_id))

    def resume(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row, execution=True)
        with lock(self.directory(job_id) / "host-writer.lock") as writer:
            row = self.repo.row(job_id)
            if row["status"] == "queued":
                return self.view(row)
            require(
                self.repo.view(row).can_resume,
                "M2_JOB_NOT_RESUMABLE",
                "Task is still active or already succeeded",
            )
            proof = None
            if row["attempt_count"]:
                proof = self.executor.immediate(
                    "inspect",
                    self.directory(job_id),
                    manifest.request.window_id,
                    "resume-" + uuid.uuid4().hex,
                    manifest.session_file_sha256,
                    lock_fds=(writer.fileno(),),
                )
            return self.view(self.repo.resume(job_id, proof))
