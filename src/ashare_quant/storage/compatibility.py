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


REQUIRED_TABLES = {"tasks", "task_logs", "task_service", "python_sources", "runs", "strategies"}


def _validate(db, path):
    objects = {
        name: kind
        for kind, name in db.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    if any(objects.get(name) != "table" for name in REQUIRED_TABLES):
        raise DatabaseCompatibilityError(
            "M2_DATABASE_SCHEMA_INCOMPATIBLE",
            "Unsupported control schema; preserve the original database",
            details={"database": str(path), "database_modified": False},
        )


@contextmanager
def compatible_snapshot(path):
    target = Path(path).resolve()
    if not target.exists():
        raise DatabaseCompatibilityError(
            "DATABASE_FILE_CHANGED",
            "Database was removed during admission",
            details={"database_modified": False},
        )
    wal = Path(str(target) + "-wal")
    query = "?mode=ro" if (wal.exists() and wal.stat().st_size > 0) else "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(target.as_uri() + query, uri=True)) as db:
            _validate(db, target)
            yield db
    except DatabaseCompatibilityError:
        raise
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        raise DatabaseCompatibilityError(
            "DATABASE_COMPATIBILITY_UNREADABLE",
            "无法读取任务库版本；请保留原库，用创建它的原固定后端检查。当前服务未初始化此库。",
            details={"database": str(path), "database_modified": False},
        ) from exc


def require_compatible_database(path):
    with compatible_snapshot(path):
        pass
