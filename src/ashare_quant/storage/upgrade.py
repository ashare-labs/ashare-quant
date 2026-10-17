"""Explicit side-by-side upgrade. Old immutable jobs retain their original executor."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from ashare_quant.contracts import DomainError


def _inventory(database):
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-journal")]
    task_root = Path(str(database) + ".tasks")
    if task_root.is_symlink():
        raise DomainError("DATABASE_UPGRADE_LINK_REFUSED", "Task directory must not be linked")
    if task_root.exists():
        paths += sorted(task_root.rglob("*"))
    result = {}
    for path in paths:
        if path.is_symlink():
            raise DomainError("DATABASE_UPGRADE_LINK_REFUSED", "Upgrade does not follow links")
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[str(path.relative_to(database.parent))] = {
                "sha256": digest,
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
    return result


def upgrade_copy(source, destination):
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise DomainError("DATABASE_UPGRADE_SOURCE", "Provide a closed regular source database")
    if destination.exists() or destination.is_symlink():
        raise DomainError(
            "DATABASE_UPGRADE_DESTINATION_EXISTS", "Choose a new empty destination path"
        )
    source = source.resolve()
    destination = destination.resolve()
    task_root = Path(str(source) + ".tasks")
    if destination.is_relative_to(task_root):
        raise DomainError(
            "DATABASE_UPGRADE_DESTINATION", "Destination must be outside the old task directory"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = _inventory(source)
    with tempfile.TemporaryDirectory(prefix=".aq-upgrade-", dir=destination.parent) as staging:
        root = Path(staging)
        archive = root / "archive"
        archive.mkdir()
        for name in before:
            target = archive / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source.parent / name, target)
        if _inventory(source) != before:
            raise DomainError(
                "DATABASE_UPGRADE_BUSY",
                "Source changed; stop the old service and workers before retrying",
            )
        if _inventory(archive / source.name) != before:
            raise DomainError(
                "DATABASE_UPGRADE_COPY_CHANGED", "Archive copy did not match source inventory"
            )
        # Open only the disposable copy. Consolidate WAL into a separate read archive.
        archived_db = archive / source.name
        consolidated = root / "historical.sqlite3"
        try:
            with (
                closing(sqlite3.connect(archived_db)) as old,
                closing(sqlite3.connect(consolidated)) as target,
            ):
                if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("integrity check failed")
                old.backup(target)
        except sqlite3.Error as exc:
            raise DomainError(
                "DATABASE_UPGRADE_UNREADABLE", "Source snapshot failed integrity verification"
            ) from exc
        # The byte-for-byte archive remains the audit input, including WAL.
        # SQLite may have checkpointed the disposable archive; restore those bytes.
        for name in before:
            shutil.copy2(source.parent / name, archive / name)
        for suffix in ("-shm", "-wal", "-journal"):
            sidecar = Path(str(archived_db) + suffix)
            if str(sidecar.relative_to(archive)) not in before:
                sidecar.unlink(missing_ok=True)
        from ashare_quant.storage.store import Store
        from ashare_quant.tasks.daily.service import Jobs

        Jobs(Store(root / "control.sqlite3"))
        if _inventory(source) != before or _inventory(archive / source.name) != before:
            raise DomainError(
                "DATABASE_UPGRADE_BUSY", "Source changed during upgrade; no new database published"
            )
        report = {
            "schema_version": "m2-database-upgrade.v1",
            "source_database": str(source),
            "source_modified": False,
            "archive": "archive/" + source.name,
            "consolidated_read_archive": "historical.sqlite3",
            "new_database": "control.sqlite3",
            "old_tasks_migrated": False,
            "old_task_execution_authorized": False,
            "original_executor_required": True,
            "files": before,
        }
        (root / "upgrade.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        from ashare_quant.storage.artifacts import sync_dir

        for file in root.rglob("*"):
            if file.is_file():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            sync_dir(directory)
        sync_dir(root)
        # mkdir reserves the destination without overwriting a concurrent operator's files.
        destination.mkdir(mode=0o700)
        try:
            # Publish the success receipt last; partial transfer never has a success receipt.
            for item in sorted(root.iterdir(), key=lambda p: p.name == "upgrade.json"):
                os.replace(item, destination / item.name)
            sync_dir(destination)
            sync_dir(destination.parent)
        except BaseException:
            # Keep any already transferred files for operator inspection; never delete them.
            raise
    return report
