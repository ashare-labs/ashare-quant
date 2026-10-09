"""Two trusted pure strategies: no native context, I/O, clock or account mutation."""

from decimal import Decimal

from ashare_quant.contracts import DomainError

from .contracts import Signal, StrategyDefinition


def evaluate(definition: StrategyDefinition, *, previous_close: str | None) -> Signal:
    if definition.template == "buy_hold.v1":
        if previous_close is not None:
            raise DomainError("M2_UNUSED_PRICE", "buy_hold does not consume AD08", 422)
        return Signal(action="target_shares", target_shares=definition.target_shares, reason="hold_template")
    if previous_close is None:
        raise DomainError("M2_PREVIOUS_CLOSE_REQUIRED", "Band strategy requires a verified facade value", 422)
    if type(previous_close) is not str:
        raise DomainError("M2_INVALID_NATIVE_VALUE", "Use the facade's normalized decimal string", 422)
    try:
        value = Decimal(previous_close)
        if not value.is_finite() or value <= 0:
            raise ValueError
    except (ValueError, ArithmeticError, TypeError):
        raise DomainError("M2_INVALID_NATIVE_VALUE", "Previous close must be positive and finite", 422) from None
    if value <= Decimal(definition.enter_below):
        return Signal(action="target_shares", target_shares=definition.target_shares, reason="enter_threshold")
    if value >= Decimal(definition.exit_above):
        return Signal(action="target_shares", target_shares=0, reason="exit_threshold")
    return Signal(action="no_change", reason="inside_band")
