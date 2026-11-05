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
    require_free_space(jobs.root, jobs.repo.policy,
                       additional_bytes=jobs.repo.policy.artifact_bytes)
