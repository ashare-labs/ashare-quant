"""Separate per-task, retained-history and real free-space storage budgets."""

import os
import shutil
import stat
from pathlib import Path

from ashare_quant.contracts import DomainError


def artifact_size(root):
    """Logical bytes currently retained by one task, including atomic temporaries."""
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(directory) / name).is_symlink()]
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue  # An atomic writer may have just renamed its temporary.
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def require_free_space(root, policy, *, reserved_bytes=0, additional_bytes=0):
    free = shutil.disk_usage(root).free
    required = policy.disk_free_bytes + reserved_bytes + additional_bytes
    if free < required:
        raise DomainError(
            "M2_JOB_STORAGE_RESERVE",
            f"Insufficient free disk space: {free} bytes available; {required} bytes required "
            "for the configured reserve and pending task budgets",
            507,
            details={"resource": "disk_free_bytes", "observed": free, "required": required,
                     "reserve": policy.disk_free_bytes, "reserved_bytes": reserved_bytes,
                     "additional_bytes": additional_bytes},
        )


def check_task_storage(root, policy, *, additional_bytes=0, host_policy=None):
    used = artifact_size(root)
    projected = used + additional_bytes
    if projected > policy.artifact_bytes:
        raise DomainError(
            "M2_JOB_ARTIFACT_LIMIT",
            f"This task's artifacts require {projected} bytes, exceeding its configured "
            f"artifact_bytes limit ({policy.artifact_bytes}); no complete result was published",
            409,
            details={"resource": "artifact_bytes", "scope": "per_task", "observed": used,
                     "additional_bytes": additional_bytes, "limit": policy.artifact_bytes},
        )
    require_free_space(root, host_policy or policy, additional_bytes=additional_bytes)


def admit_storage(jobs):
    # Creation holds the shared admission lock. Completed history already consumes
    # filesystem space, but must never consume another task's artifact allowance.
    scope, parameters = jobs.repo.queue_scope()
    with jobs.repo.store.connection() as db:
        pending = [(row[0], row[1]) for row in db.execute(
            "SELECT job_id,manifest FROM tasks WHERE " + scope +
            " AND status IN ('queued','running','cancelling')", parameters)]
    # One metadata inventory at admission, never a repeated history scan in the
    # worker polling loop. Contents and historical database rows stay untouched.
    sizes = {path.name: artifact_size(path) for path in jobs.root.glob("m2job-*")
             if path.is_dir() and not path.is_symlink()}
    import json

    remaining = sum(max(0, jobs.execution_policy(json.loads(manifest)).artifact_bytes - sizes.get(job_id, 0))
                    for job_id, manifest in pending)
    retained = sum(sizes.values())
    projected = retained + remaining + jobs.repo.policy.artifact_bytes
    if projected > jobs.repo.policy.retained_artifact_bytes:
        raise DomainError(
            "M2_JOB_RETENTION_LIMIT",
            f"Retained task artifacts and reserved task budgets require {projected} bytes, "
            f"exceeding retained_artifact_bytes ({jobs.repo.policy.retained_artifact_bytes}); "
            "no history was deleted",
            409,
            details={"resource": "retained_artifact_bytes", "scope": "all_retained_tasks",
                     "observed": retained, "reserved_bytes": remaining,
                     "additional_bytes": jobs.repo.policy.artifact_bytes,
                     "limit": jobs.repo.policy.retained_artifact_bytes},
        )
    require_free_space(jobs.root, jobs.repo.policy, reserved_bytes=remaining,
                       additional_bytes=jobs.repo.policy.artifact_bytes)
