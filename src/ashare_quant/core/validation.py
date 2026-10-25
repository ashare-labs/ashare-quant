"""Input validation shared by daily and minute consumers.

The input digest uses ASCII-escaped JSON, unlike storage.artifacts.digest.
Legacy MINUTE error codes are part of the existing daily/minute interface.
"""

import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal

from ashare_quant.contracts import SHANGHAI


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def stamp(value):
    require(isinstance(value, str), "MINUTE_AWARE_TIMESTAMP_REQUIRED")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "MINUTE_AWARE_TIMESTAMP_REQUIRED")
    return result.astimezone(SHANGHAI)


def number(value, *, zero=False):
    require(type(value) in (str, int, float), "MINUTE_NUMBER_TYPE")
    n = Decimal(str(value))
    require(
        n.is_finite() and (n >= 0 if zero else n > 0) and n < Decimal("1e15"),
        "MINUTE_INVALID_NUMBER",
    )
    require(n == 0 or float(n) >= sys.float_info.min, "MINUTE_NUMBER_UNDERFLOW")
    return n
