"""Native number normalization and durable JSON writes."""
import json
import os
from decimal import Decimal, localcontext
from pathlib import Path


def money(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("NONFINITE_NATIVE_MONEY")
    # RQAlpha uses float internally. Wire normalization is declared, not source decimal recovery.
    with localcontext() as context:
        context.prec = 64
        return format(number.quantize(Decimal("0.00000001")), "f").rstrip("0").rstrip(".") or "0"


def integer(value):
    if int(value) != value:
        raise ValueError("FRACTIONAL_NATIVE_SHARES_UNSUPPORTED")
    return int(value)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".pending")
    with temp.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
