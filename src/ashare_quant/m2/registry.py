"""Immutable template/parameter registration in the existing local control DB."""

import hashlib
from datetime import datetime, timezone
from importlib.resources import files

from pydantic import ValidationError

from ashare_quant.contracts import DomainError

from .common import canonical, digest, require
from .contracts import StrategyDefinition, StrategyVersion


def template_source():
    return files("ashare_quant.m2").joinpath("strategy.py").read_text()


def source_sha256():
    return hashlib.sha256(template_source().encode()).hexdigest()


def version_identity(definition, source_sha):
    return digest({"api": "m2.daily.v2", "definition": definition, "template_source_sha256": source_sha})


class StrategyRegistry:
    def __init__(self, store):
        self.store = store

    def register(self, definition: StrategyDefinition) -> StrategyVersion:
        source = source_sha256()
        value = StrategyVersion(version_id=version_identity(definition, source), definition=definition,
                                source_sha256=source, parameter_sha256=digest(definition),
                                created_at=datetime.now(timezone.utc))
        with self.store.connection(transaction=True) as db:
            db.execute("INSERT OR IGNORE INTO m2_strategies(version_id,body) VALUES (?,?)",
                       (value.version_id, canonical(value)))
            row = db.execute("SELECT body FROM m2_strategies WHERE version_id=?", (value.version_id,)).fetchone()
        return self._decode(row[0], value.version_id)

    @staticmethod
    def _decode(raw, version_id):
        try:
            result = StrategyVersion.model_validate_json(raw)
        except (ValidationError, ValueError):
            raise DomainError("M2_REGISTRY_CORRUPT", "Stored strategy is invalid", 409) from None
        require(result.version_id == version_id == version_identity(result.definition, result.source_sha256)
                and result.parameter_sha256 == digest(result.definition), "M2_REGISTRY_CORRUPT", "Strategy identity differs")
        require(result.source_sha256 == source_sha256(), "M2_STRATEGY_SOURCE_CHANGED", "Use the strategy's fixed template source")
        return result

    def get(self, version_id):
        with self.store.connection() as db:
            row = db.execute("SELECT body FROM m2_strategies WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise DomainError("M2_STRATEGY_NOT_FOUND", "Unknown M2 strategy version", 404)
        return self._decode(row[0], version_id)

    def list(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT version_id,body FROM m2_strategies ORDER BY version_id").fetchall()
        return tuple(self._decode(row[1], row[0]) for row in rows)
