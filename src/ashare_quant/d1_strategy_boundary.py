"""Predefined clock-only intent selection and native outcome stop conditions.

No account arithmetic, order submission, dataset handle or native context is
available to the selector. Native outcomes are validation inputs, not a ledger.
"""

from typing import Literal

from pydantic import AwareDatetime, Field

from .contracts import Contract, DomainError
from .d1 import PreviewRequest, plan, preview


class NativeOutcome(Contract):
    order_id: str = Field(min_length=1)
    security: str
    submitted_at: AwareDatetime
    side: Literal["buy", "sell"]
    quantity: int = Field(strict=True, ge=0)
    filled: int = Field(strict=True, ge=0)
    status: Literal["active", "filled", "partial", "rejected", "cancelled"]


def select_predefined_intent(request: PreviewRequest, *, native_context=None, bar_dict=None,
                             current_snapshot=None, price_board=None, history_bars=None):
    # Deliberately do not even inspect, log, truth-test or iterate the native inputs.
    return preview(request)


def check_native_outcome(index: int, outcome: NativeOutcome) -> None:
    if type(index) is not int or index not in (0, 1):
        raise DomainError("BR1_UNPLANNED_ORDER", "Only the two ordered frozen intents are allowed", 422)
    expected = plan().intents[index]
    if (outcome.security, outcome.submitted_at, outcome.side, outcome.quantity) != (
            expected.security, expected.at, expected.side, expected.quantity):
        raise DomainError("BR1_UNPLANNED_ORDER", "Native order differs from the frozen intent", 422)
    if outcome.status != "filled" or outcome.filled != expected.quantity:
        raise DomainError("BR1_INTENT_NOT_FULLY_FILLED", "Stop the profile without changing date or quantity", 422,
                          details={"order_id": outcome.order_id, "filled": outcome.filled,
                                   "status": outcome.status})


def check_native_pair(outcomes: tuple[NativeOutcome, ...]) -> None:
    if len(outcomes) != 2 or outcomes[0].order_id == outcomes[1].order_id:
        raise DomainError("BR1_UNPLANNED_ORDER", "Exactly two distinct native order identities are required", 422)
    for index, outcome in enumerate(outcomes):
        check_native_outcome(index, outcome)
