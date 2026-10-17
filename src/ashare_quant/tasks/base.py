"""Shared task queue lifecycle, guarded files, cancellation and resumption."""
import uuid

from pydantic import TypeAdapter

from ashare_quant.identity import source_sha256
from ashare_quant.storage.artifacts import lock, require
from ashare_quant.tasks.daily.contracts import JobID, JobLogs
from ashare_quant.tasks.daily.executor import Configuration
from ashare_quant.tasks.repository import Repository


class TaskService:
    def __init__(self, store, configuration=None):
        self.config = configuration or Configuration.environment()
        self.root = store.path.with_name(store.path.name + ".tasks")
        require(not self.root.is_symlink())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repo = Repository(store, self.config.policy)
        self.source = source_sha256()

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
                proof = self.executor.immediate(
                    "inspect",
                    self.directory(job_id),
                    manifest.request.window_id,
                    "resume-" + uuid.uuid4().hex,
                    manifest.session_file_sha256,
                    lock_fds=(writer.fileno(),),
                )
            return self.view(self.repo.resume(job_id, proof))
