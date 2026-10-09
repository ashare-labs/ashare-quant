"""Durable local artifacts and advisory locks, shared by API and worker."""

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from ashare_quant.contracts import DomainError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return sha(canonical(value))


def require(ok, code="M2_JOB_IDENTITY_CONFLICT", message="Frozen task identity differs"):
    if not ok:
        raise DomainError(code, message, 409)


def regular(path):
    path = Path(path)
    require(
        not path.is_symlink() and path.is_file(),
        "M2_JOB_ARTIFACT_UNAVAILABLE",
        "Required local artifact is missing or linked",
    )
    return path.read_bytes()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, value):
    path = Path(path)
    raw = value if isinstance(value, bytes) else canonical(value)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    with tmp.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)


@contextmanager
def lock(path, *, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), "M2_JOB_LOCK_CONFLICT", "Lock must be a local regular file")
    stream = path.open("a+b")
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise DomainError(
                "M2_JOB_WRITER_BUSY", "An existing task writer still owns the local account", 409
            ) from None
        # Child retains this open-file-description lock after a worker crash.
        yield stream
    finally:
        # Do not explicitly LOCK_UN: an orphan child may still share this fd.
        stream.close()
