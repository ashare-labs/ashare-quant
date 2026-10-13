"""Stable HTTP/CLI storage refusals with safe idempotent retry guidance."""

import errno
import sqlite3

from .contracts import DomainError


def storage_error(exc):
    sql_code = getattr(exc, "sqlite_errorcode", 0) & 0xff
    os_code = getattr(exc, "errno", None)
    if os_code in {errno.ENOSPC, errno.EDQUOT} or sql_code == sqlite3.SQLITE_FULL:
        code, message = "STORAGE_FULL", "Storage capacity exhausted; preserve task evidence and restore free space"
    elif os_code in {errno.EACCES, errno.EPERM, errno.EROFS} or sql_code in {sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY}:
        code, message = "STORAGE_PERMISSION_DENIED", "Storage is not writable; restore the configured directory permissions"
    elif sql_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        code, message = "DATABASE_BUSY", "Database busy; retry later"
    elif isinstance(exc, sqlite3.Error):
        code, message = "DATABASE_OPERATION_REFUSED", "Database operation refused; preserve the database for inspection"
    else:
        code, message = "STORAGE_UNAVAILABLE", "Storage operation failed; preserve task evidence and inspect local storage"
    return DomainError(code, message, 409 if code == "DATABASE_OPERATION_REFUSED" else 503,
                       details={"os_errno": os_code, "sqlite_errorcode": sql_code or None,
                                "retry": "After repairing storage, retry the identical request with the original idempotency_key; an accepted task is reused",
                                "preserve_database_and_task_directory": True,
                                "acceptance": "A failed response does not determine whether a preceding commit completed; do not submit a new key"})
