"""Small trusted-local strategy API. This is not an adversarial code sandbox.

Only the supplied decision snapshot is supported. Namespace state is discarded
between decisions; external effects and unpinned dependencies are unsupported.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PriorAccount:
    cash: Decimal
    quantity: int
    snapshot_as_of: datetime
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class DecisionContext:
    logical_at: datetime
    trading_date: date
    previous_open: date
    day_index: int
    security: str
    prior_account: PriorAccount


@dataclass(frozen=True, slots=True)
class DecisionData:
    previous_close: Decimal
    price_date: date
    data_binding_sha256: str
    read_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class TargetShares:
    quantity: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NoChange:
    reason: str = ""
