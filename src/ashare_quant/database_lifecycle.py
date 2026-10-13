"""Single-host database admission without writable preflight on user databases."""

import fcntl
import hashlib
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .contracts import DomainError
from .database_schema import SCHEMA


def file_identity(path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino


@contextmanager
def database_lock(path, *, exclusive=False):
    """Stable process/thread lock outside the protected database directory.

    Every Store connection participates. Never unlink these locks: unlinking a
    live lock could let a second process lock a different inode for the same DB.
    Old backends and external SQLite clients must be stopped during admission.
    """
    root = Path(tempfile.gettempdir()) / f"ashare-quant-db-locks-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise DomainError("DATABASE_LOCK_UNAVAILABLE", "Database admission lock directory is unsafe", 503)
    name = hashlib.sha256(os.fsencode(path)).hexdigest() + ".lock"
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise DomainError("DATABASE_LOCK_UNAVAILABLE", "Database admission lock is unsafe", 503)
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DomainError("DATABASE_BUSY", "Database admission is busy; retry later", 503) from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def initialize_database(path):
    """Build and verify privately; publish once without replacing any existing file."""
    from .database_compat import require_compatible_database
    from .m2_jobs.files import sync_dir

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal", "-shm")):
        raise DomainError("DATABASE_INITIALIZATION_CONFLICT", "Preserve existing database links and sidecars; choose a new path")
    with tempfile.TemporaryDirectory(prefix=".aq-initialize-", dir=path.parent) as directory:
        staged = Path(directory) / "control.sqlite3"
        with closing(sqlite3.connect(staged)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
        require_compatible_database(staged)
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            # link() is create-only; replace()/rename() could overwrite a rival file.
            os.link(staged, path)
        except FileExistsError:
            # Caller validates the winner. Never open it to finish our initialization.
            return
        sync_dir(path.parent)
