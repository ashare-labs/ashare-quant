"""Local execution inputs; no market data, quality calculation or accounting."""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from ashare_quant.contracts import SHANGHAI
from ashare_quant.m2.common import digest


@dataclass(frozen=True)
class Case:
    case_id: str
    security: str
    day: date
    initial_cash: str
    quantity: int
    style: str
    volume_ratio: str
    submit_at: str = "10:00"

    def to_dict(self):
        return {**asdict(self), "day": self.day.isoformat()}

    def at(self, clock):
        return datetime.fromisoformat(f"{self.day}T{clock}").replace(tzinfo=SHANGHAI)

    @property
    def strategy_path_name(self):
        return self.case_id.lower() + ".py"


CASES = {
    name: Case(name, "000002.XSHE" if name == "B11" else "000001.XSHE",
               date(2016, 1, 4) if name == "B11" else date(2024, 3, 4),
               "100000000" if name in {"B10", "B13"} else "1000000",
               100000 if name in {"B10", "B13"} else 100,
               "limit" if name in {"B08", "B10", "B12"} else "market",
               "0.0001" if name in {"B10", "B13"} else "1.0",
               "09:20" if name == "B12" else "10:00")
    for name in ("B08", "B09", "B10", "B11", "B12", "B13")
}


@dataclass(frozen=True)
class Read:
    """Owner response and provenance carried together; no result inference."""

    operation: str
    logical_at: datetime
    payload_json: str
    evidence_json: str

    @classmethod
    def of(cls, operation, logical_at, value, evidence):
        if logical_at.tzinfo is None or not isinstance(evidence, dict) or not evidence:
            raise ValueError("SIX_OWNER_READ_IDENTITY_REQUIRED")
        return cls(operation, logical_at, json.dumps(value, sort_keys=True),
                   json.dumps(evidence, sort_keys=True))

    @property
    def value(self):
        return json.loads(self.payload_json)

    def to_dict(self):
        return {"operation": self.operation, "logical_at": self.logical_at.isoformat(),
                "value": self.value, "evidence": json.loads(self.evidence_json)}

    @property
    def identity(self):
        return digest(self.to_dict())


class Provider(Protocol):
    """Implemented by a public AD adapter, or an explicitly synthetic test fixture.

    descriptor carries dataset_id, manifest, quality and scope; bootstrap carries
    bounded native metadata. All prices/facts remain owner queries with the actual
    logic clock. `capacity` is engine-only retrospective whole-day volume.
    """

    def descriptor(self) -> dict: ...
    def bootstrap(self, case: Case) -> dict: ...
    def read(self, operation: str, logical_at: datetime, **parameters) -> Read: ...


def number(value):
    """Native float projection as decimal text, never rounded to reference values."""
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("SIX_NONFINITE_NATIVE_VALUE")
    return format(result, "f")


def owner_price(value):
    """Validate the numeric wire domain before handing a scalar to native code."""
    if type(value) is not str:
        raise ValueError("SIX_OWNER_PRICE_TYPE")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("SIX_OWNER_PRICE_VALUE")
    return float(parsed)


def owner_volume(value):
    # Native NaN means unlimited liquidity; never let unknown/coerced volume in.
    if type(value) is not int or value < 0:
        raise ValueError("SIX_OWNER_VOLUME_TYPE")
    return value


def event_plan(case):
    clocks = [("before", "09:00")]
    if case.case_id == "B12":
        clocks += [("callback", x) for x in ("09:20", "09:25", "09:28")]
        clocks += [("opening", "09:30"), ("bar", "09:31"), ("bar", "09:32")]
    else:
        clocks += [("bar", "10:00")]
    clocks += [("bar", "10:01"), ("after", "15:30"), ("end_clock", "23:59:59")]
    return tuple({"index": i, "kind": kind, "at": case.at(at).isoformat()}
                 for i, (kind, at) in enumerate(clocks, 1))
