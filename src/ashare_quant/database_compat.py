"""Check persisted task contracts before opening the control database for writing.

SQLite user_version describes the control tables, not the M2 task contract. Inspect
a temporary copy so even SQLite WAL recovery cannot change historical files.
"""

import json
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .contracts import DomainError

EXPECTED_MANIFEST = "m2-job-manifest.v6"


class DatabaseCompatibilityError(DomainError):
    """The service can expose this refusal without initializing any repositories."""


def _signature(paths):
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return result


def require_compatible_database(path: Path) -> None:
    if not path.exists():
        return
    # Copy WAL/journal as well: immutable=1 against the original would miss tasks
    # committed only to WAL. SHM is rebuilt exclusively in the disposable copy.
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-journal")]
    for attempt in range(12):
        if attempt:
            time.sleep(0.01)
        with tempfile.TemporaryDirectory(prefix="ashare-quant-db-check-") as directory:
            snapshot = Path(directory) / "control.sqlite3"
            copies = [snapshot, Path(str(snapshot) + "-wal"), Path(str(snapshot) + "-journal")]
            before = _signature(paths)
            try:
                for source, target, signature in zip(paths, copies, before):
                    if signature is not None:
                        shutil.copyfile(source, target)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DatabaseCompatibilityError(
                    "DATABASE_COMPATIBILITY_UNREADABLE", "Cannot copy the database for a read-only compatibility check",
                    details={"database_modified": False}) from exc
            if before != _signature(paths):
                continue
            if before[0] is None or before[0][1] == 0:
                return
            try:
                with closing(sqlite3.connect(snapshot)) as db:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, 1, 2, 3):
                        raise DatabaseCompatibilityError(
                            "DATABASE_SCHEMA_INCOMPATIBLE", "Unsupported control schema; preserve the original database",
                            details={"detected_schema": version, "database_modified": False})
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                        "AND name='m2_jobs'").fetchone()
                    if exists is None:
                        return
                    for table, required_columns in {
                        "m2_job_logs": {"sequence", "job_id", "at", "event", "details"},
                        "m2_job_service": {"singleton", "policy", "policy_sha256"},
                    }.items():
                        actual = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                        if actual and required_columns - actual:
                            raise DatabaseCompatibilityError(
                                "M2_DATABASE_SCHEMA_INCOMPATIBLE", "Incomplete task service table",
                                details={"table": table, "missing_columns": sorted(required_columns - actual), "database_modified": False})
                    columns = {r[1] for r in db.execute("PRAGMA table_info(m2_jobs)")}
                    if "manifest" not in columns:
                        raise DatabaseCompatibilityError("M2_DATABASE_SCHEMA_INCOMPATIBLE", "Task table has no manifest column", details={"database_modified": False})
                    detected = set()
                    backends = set()
                    sources = set()
                    for (raw,) in db.execute("SELECT manifest FROM m2_jobs"):
                        manifest = json.loads(raw)
                        detected.add(manifest.get("schema_version", "unknown"))
                        sources.add(manifest.get("service_identity", {}).get("source_sha256"))
                        version = manifest.get("service_identity", {}).get("backend_version")
                        if isinstance(version, str):
                            backends.add(version)
            except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
                raise DatabaseCompatibilityError(
                    "DATABASE_COMPATIBILITY_UNREADABLE",
                    "无法读取任务库版本；请保留原库，用创建它的原固定后端检查。当前服务未初始化此库。",
                    details={"database": str(path), "database_modified": False},
                ) from exc
            if detected - {EXPECTED_MANIFEST}:
                raise DatabaseCompatibilityError(
                    "M2_DATABASE_VERSION_INCOMPATIBLE",
                    "此任务库包含旧版或不支持的任务清单，不能用于当前 v6 服务。"
                    "请保留旧库，使用创建它的原固定后端读取；为 v6 选择新的数据库路径。不会自动迁移或执行旧任务。",
                    details={
                        "database": str(path),
                        "expected_manifest_version": EXPECTED_MANIFEST,
                        "detected_manifest_versions": sorted(str(v) for v in detected),
                        "detected_backend_versions": sorted(backends),
                        "database_modified": False,
                        "automatic_migration": False,
                        "old_task_execution": False,
                        "remediation": ["preserve_database_and_task_directory",
                                        "read_with_original_pinned_backend",
                                        "use_new_database_path_for_v6"],
                    },
                )
            if detected:
                from .identity import source_sha256
                if sources != {source_sha256()}:
                    raise DatabaseCompatibilityError(
                        "M2_DATABASE_EXECUTOR_INCOMPATIBLE",
                        "Tasks require their original fixed backend; preserve the database and use database-upgrade for a separate new run database",
                        details={"database_modified": False, "old_task_execution": False,
                                 "detected_source_sha256": sorted(str(v) for v in sources)})
            required = {
                "job_id", "idempotency_key", "request_hash", "request", "manifest", "manifest_sha256",
                "account_id", "status", "created_at", "updated_at", "attempt_count", "lease_token",
                "lease_until", "cancellation_requested_at", "committed_cursor", "checkpoint_complete",
                "checkpoint_sha256", "result", "error",
            }
            if required - columns:
                raise DatabaseCompatibilityError(
                    "M2_DATABASE_SCHEMA_INCOMPATIBLE",
                    "Task table schema is incomplete, including empty legacy tables; preserve it and choose a new database",
                    details={"missing_columns": sorted(required - columns), "database_modified": False})
            return
    raise DatabaseCompatibilityError(
        "DATABASE_COMPATIBILITY_BUSY",
        "任务库正在变化，无法完成只读版本检查。请停止使用此库的原服务和 worker 后重试，并保留全部数据库文件。",
        details={"database": str(path), "database_modified": False},
    )
