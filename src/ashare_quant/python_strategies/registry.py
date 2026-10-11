"""Local CLI registration: metadata and exact source bytes in one transaction."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.m2.common import canonical, digest
from ashare_quant.m2.contracts import StrategyVersion
from ashare_quant.m2.registry import StrategyRegistry, version_identity

from .spec import PythonDefinition, check_source, is_python, sdk_sha256


def register(store, source_path, descriptor):
    try:
        with Path(source_path).open("rb") as source:
            raw = source.read(65537)
    except OSError as exc:
        raise DomainError(
            "PYTHON_SOURCE_UNAVAILABLE", "Cannot read the local strategy file", 422
        ) from exc
    check_source(raw, descriptor)
    source_sha = hashlib.sha256(raw).hexdigest()
    definition = PythonDefinition(
        **descriptor.model_dump(), source_sha256=source_sha, sdk_sha256=sdk_sha256()
    )
    version = StrategyVersion(
        version_id=version_identity(definition, source_sha),
        definition=definition,
        source_sha256=source_sha,
        parameter_sha256=digest(definition),
        created_at=datetime.now(timezone.utc),
    )
    with store.connection(transaction=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO m2_strategies VALUES (?,?)",
            (version.version_id, canonical(version)),
        )
        db.execute(
            "INSERT OR IGNORE INTO m2_python_sources VALUES (?,?)", (version.version_id, raw)
        )
        body = db.execute(
            "SELECT body FROM m2_strategies WHERE version_id=?", (version.version_id,)
        ).fetchone()[0]
        saved = db.execute(
            "SELECT source FROM m2_python_sources WHERE version_id=?", (version.version_id,)
        ).fetchone()[0]
        if saved != raw:
            raise DomainError("PYTHON_SOURCE_CHANGED", "Registered source differs")
    return StrategyRegistry._decode(body, version.version_id)


def source_bytes(store, strategy):
    if not is_python(strategy):
        return None
    with store.connection() as db:
        row = db.execute(
            "SELECT source FROM m2_python_sources WHERE version_id=?", (strategy.version_id,)
        ).fetchone()
    if row is None or hashlib.sha256(row[0]).hexdigest() != strategy.source_sha256:
        raise DomainError(
            "PYTHON_SOURCE_CHANGED", "Registered source is unavailable; cannot create a new task"
        )
    return row[0]
