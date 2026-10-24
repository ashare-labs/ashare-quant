"""Shared task queue lifecycle, guarded files, cancellation and resumption."""
import uuid
from copy import copy

from pydantic import TypeAdapter

from ashare_quant.identity import source_sha256
from ashare_quant.storage.artifacts import lock, require
from ashare_quant.tasks.contracts import JobID, JobLogs
from ashare_quant.tasks.executor import Configuration
from ashare_quant.tasks.policy import ServicePolicy
from ashare_quant.tasks.repository import Repository


class TaskService:
    active_queue = False

    def __init__(self, store, configuration=None):
        self.config = configuration or Configuration.environment()
        self.root = store.path.with_name(store.path.name + ".tasks")
        require(not self.root.is_symlink())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.source = source_sha256()
        self.repo = Repository(store, self.config.policy,
                               execution_source=self.source if self.active_queue else None)

    def executor_args(self, manifest):
        """Optional executor-specific positional arguments; active tasks need none."""
        return ()

    def execution_policy(self, manifest):
        session = manifest.session if hasattr(manifest, "session") else manifest["session"]
        value = session.get("resource_policy", session.get("data_budget"))
        # Older factor/retired manifests predate per-task policy snapshots. Their
        # database originally forbade any policy change, so its initial policy is
        # the authoritative fallback, never today's environment override.
        return ServicePolicy.from_frozen(value) if value is not None else self.repo.initial_policy

    def for_execution(self, manifest):
        target = copy(self)
        target.repo = copy(self.repo)
        target.repo.policy = self.execution_policy(manifest)
        target.executor = type(self.executor)(self.root, self.config, target.repo.policy)
        target.host_policy = self.repo.policy
        return target

    def guard(self):
        require(
            source_sha256() == self.source,
            "M2_JOB_SERVICE_CHANGED",
            "Service changed after startup; use the frozen task version",
        )

    def directory(self, job_id):
        job_id = TypeAdapter(JobID).validate_python(job_id)
        require(not self.root.is_symlink())
        path = self.root / job_id
        require(not path.is_symlink())
        return path

    def unaccepted_directories(self):
        accepted = {row["job_id"] for row in self.repo.rows()}
        return sum(path.name not in accepted for path in self.root.glob("m2job-*"))

    def service_status(self):
        self.guard()
        return self.repo.status().model_copy(update={
            "unaccepted_task_directories": self.unaccepted_directories(),
        })

    def get(self, job_id):
        return self.view(self.repo.row(job_id))

    def logs(self, job_id):
        self.check(self.repo.row(job_id))
        return JobLogs(job_id=job_id, items=tuple(self.repo.logs(job_id)))

    def cancel(self, job_id):
        self.check(self.repo.row(job_id))
        return self.view(self.repo.cancel(job_id))

    def resume(self, job_id):
        row = self.repo.row(job_id)
        manifest = self.check(row, execution=True)
        execution = self.for_execution(manifest)
        with lock(self.root / "creation.lock", timeout=self.repo.policy.admission_wait_seconds), \
                lock(self.directory(job_id) / "host-writer.lock") as writer:
            row = self.repo.row(job_id)
            if row["status"] == "queued":
                return self.view(row)
            require(
                self.repo.view(row).can_resume,
                "M2_JOB_NOT_RESUMABLE",
                "Task is still active or already succeeded",
            )
            if row["status"] in {"failed", "cancelled"}:
                self.repo.capacity()
            proof = None
            if row["attempt_count"]:
                proof = execution.executor.immediate(
                    "inspect",
                    self.directory(job_id),
                    *self.executor_args(manifest),
                    "resume-" + uuid.uuid4().hex,
                    manifest.session_file_sha256,
                    lock_fds=(writer.fileno(),),
                )
            return self.view(execution.repo.resume(job_id, proof))
