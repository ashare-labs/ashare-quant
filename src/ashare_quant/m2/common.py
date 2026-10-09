"""Canonical identities for M2 metadata, never market/account calculations."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from ashare_quant.contracts import DomainError


def wire(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def canonical(value):
    return json.dumps(wire(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decimal_text(value: str) -> str:
    result = format(Decimal(value), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def require(condition, code, message):
    if not condition:
        raise DomainError(code, message, 409)
