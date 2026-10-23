"""Validate a disposable SQLite snapshot before admitting any original connection."""

import json
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.storage.schema import SCHEMA

EXPECTED_MANIFEST = "m2-job-manifest.v6"


class DatabaseCompatibilityError(DomainError):
    """The service can expose this refusal without initializing any repositories."""


def _signature(paths):
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return result


def _schema(db):
    # Compare reserved DDL, including constraints, indexes, expressions and defaults,
    # not just column names. Whitespace in quoted strings remains significant.
    def normalized(sql):
        return tuple(re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|[^\w\s]", sql or ""))

    return {name: (kind, table, normalized(sql)) for kind, name, table, sql in
            db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}


@lru_cache(maxsize=1)
def _expected_schema():
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(SCHEMA)
        return _schema(db)


def _validate(db, path):
    # Scan every B-tree page, not just the job manifests. Run only against the copy.
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise DatabaseCompatibilityError("DATABASE_INTEGRITY_FAILED", "Database integrity check failed; preserve the original and use its pinned backend for inspection",
                                         details={"database_modified": False})
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, 3, 4):
        raise DatabaseCompatibilityError("DATABASE_SCHEMA_INCOMPATIBLE", "Unsupported control schema; preserve the original database",
                                         details={"detected_schema": version, "database_modified": False})
    actual = _schema(db)
    expected = _expected_schema()
    wrong_types = [name for name, value in expected.items() if name in actual and actual[name][:2] != value[:2]]
    if wrong_types:
        raise DatabaseCompatibilityError("M2_DATABASE_SCHEMA_INCOMPATIBLE", "Reserved database names have unsupported object types",
                                         details={"objects": sorted(wrong_types), "database_modified": False})
    detected, backends, sources = set(), set(), set()
    if "tasks" in actual:
        columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
        if "manifest" not in columns:
            raise DatabaseCompatibilityError("M2_DATABASE_SCHEMA_INCOMPATIBLE", "Task table has no manifest column", details={"database_modified": False})
        for (raw,) in db.execute("SELECT manifest FROM tasks"):
            manifest = json.loads(raw)
            detected.add(manifest.get("schema_version", "unknown"))
            sources.add(manifest.get("service_identity", {}).get("source_sha256"))
            backend = manifest.get("service_identity", {}).get("backend_version")
            if isinstance(backend, str):
                backends.add(backend)
    if detected - {EXPECTED_MANIFEST, "portfolio-job-manifest.v1", "python-minute-job.v1", "factor-job.v1"}:
        raise DatabaseCompatibilityError(
            "M2_DATABASE_VERSION_INCOMPATIBLE",
            "此任务库包含旧版或不支持的任务清单，不能用于当前 v6 服务。"
            "请保留旧库，使用创建它的原固定后端读取；为 v6 选择新的数据库路径。不会自动迁移或执行旧任务。",
            details={"database": str(path), "expected_manifest_version": EXPECTED_MANIFEST,
                     "detected_manifest_versions": sorted(str(v) for v in detected),
                     "detected_backend_versions": sorted(backends), "database_modified": False,
                     "automatic_migration": False, "old_task_execution": False,
                     "remediation": ["preserve_database_and_task_directory", "read_with_original_pinned_backend",
                                     "use_new_database_path_for_v6"]})
    # Source identities belong to individual frozen tasks, not the entire database.
    # A supported historical schema may be read after an upgrade. Execution still
    # checks the exact source, and the worker never claims foreign-source tasks.
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in sources):
        raise DatabaseCompatibilityError(
            "M2_DATABASE_EXECUTOR_INCOMPATIBLE", "Task source identity is missing or malformed",
            details={"database_modified": False, "old_task_execution": False})
    different = [name for name, value in expected.items() if actual.get(name) != value]
    if version != 4 or different:
        raise DatabaseCompatibilityError(
            "M2_DATABASE_SCHEMA_INCOMPATIBLE", "Incomplete or unsupported control schema; use database-upgrade to create a separate new generation",
            details={"objects": sorted(different), "detected_schema": version,
                     "database_modified": False, "automatic_migration": False})
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DatabaseCompatibilityError("DATABASE_INTEGRITY_FAILED", "Database references failed integrity verification",
                                         details={"database_modified": False})


@contextmanager
def compatible_snapshot(path):
    # Include committed WAL and hot journals. SHM is rebuilt only in the copy.
    # The caller holds database_lock to exclude cooperating Store connections.
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-journal")]
    for attempt in range(12):
        if attempt:
            time.sleep(0.01)
        with tempfile.TemporaryDirectory(prefix="ashare-quant-db-check-") as directory:
            snapshot = Path(directory) / "control.sqlite3"
            copies = [snapshot, Path(str(snapshot) + "-wal"), Path(str(snapshot) + "-journal")]
            before = _signature(paths)
            if before[0] is None:
                raise DatabaseCompatibilityError("DATABASE_FILE_CHANGED", "Database was removed during admission", details={"database_modified": False})
            try:
                for source, target, signature in zip(paths, copies, before):
                    if signature is not None:
                        shutil.copyfile(source, target)
            except FileNotFoundError:
                continue
            if before != _signature(paths):
                continue
            with closing(sqlite3.connect(snapshot)) as db:
                try:
                    _validate(db, path)
                except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
                    raise DatabaseCompatibilityError(
                        "DATABASE_COMPATIBILITY_UNREADABLE",
                        "无法读取任务库版本；请保留原库，用创建它的原固定后端检查。当前服务未初始化此库。",
                        details={"database": str(path), "database_modified": False}) from exc
                # Also check after potentially long integrity/schema/identity scans.
                if before != _signature(paths):
                    continue
                yield db
                return
    raise DatabaseCompatibilityError(
        "DATABASE_COMPATIBILITY_BUSY",
        "任务库正在变化，无法完成只读版本检查。请停止使用此库的原服务和 worker 后重试，并保留全部数据库文件。",
        details={"database": str(path), "database_modified": False})


def require_compatible_database(path):
    with compatible_snapshot(path):
        pass
