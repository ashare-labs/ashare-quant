"""Durable local artifacts and advisory locks, shared by API and worker."""

import fcntl
import hashlib
import json
import os
import time
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


class AtomicWriteUncertain(OSError):
    """Publication completed, but its directory durability could not be confirmed."""


def atomic(path, value, *, dir_fd=None, overwrite=True):
    """Publish complete bytes; optionally use an already checked directory descriptor."""
    path = Path(path)
    raw = value if isinstance(value, bytes) else canonical(value)
    if dir_fd is None:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return atomic(path.name, raw, dir_fd=fd, overwrite=overwrite)
        finally:
            os.close(fd)
    if path.name != str(path):
        raise ValueError("Descriptor-relative publication requires a single filename")
    tmp = "." + uuid.uuid4().hex + ".pending"
    published = False
    temporary_created = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dir_fd)
        temporary_created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            # link is an atomic create-if-absent; a concurrent creator cannot be overwritten.
            os.link(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    follow_symlinks=False)
        published = True
        if not overwrite:
            os.unlink(tmp, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        if published:
            raise AtomicWriteUncertain(exc.errno, "File published; durability unconfirmed") from exc
        raise
    finally:
        if temporary_created:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass  # Best-effort cleanup must not mask the publication outcome.


@contextmanager
def lock(path, *, blocking=False, timeout=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), "M2_JOB_LOCK_CONFLICT", "Lock must be a local regular file")
    stream = path.open("a+b")
    try:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking and deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    raise DomainError(
                        "M2_JOB_WRITER_BUSY" if deadline is None else "M2_JOB_ADMISSION_BUSY",
                        "An existing writer owns this task or admission slot; retry later",
                        409 if deadline is None else 503,
                    ) from None
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        # Child retains this open-file-description lock after a worker crash.
        yield stream
    finally:
        # Do not explicitly LOCK_UN: an orphan child may still share this fd.
        stream.close()


@contextmanager
def worker_slot(root, count):
    """Open-file-description locks survive host death in the child. No PID signalling."""
    for index in range(count):
        candidate = root / ("worker.lock" if index == 0 else f"worker-{index + 1}.lock")
        context = lock(candidate)
        try:
            stream = context.__enter__()
        except DomainError as exc:
            if exc.code == "M2_JOB_WRITER_BUSY":
                continue
            raise
        try:
            yield stream
        finally:
            context.__exit__(None, None, None)
        return
    raise DomainError("M2_JOB_WRITER_BUSY", "All configured execution slots are occupied", 409)
