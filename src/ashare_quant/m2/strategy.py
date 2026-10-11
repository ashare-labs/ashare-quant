"""Two trusted pure strategies: no native context, I/O, clock or account mutation."""

from decimal import Decimal

from ashare_quant.contracts import DomainError

from .contracts import Signal, StrategySpec
from .rules import select_rule


def evaluate(definition: StrategySpec, *, previous_close: str | None, prior_cash=None, prior_quantity=None, day_index=None) -> Signal:
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
    if definition.template == "decision_rules.v1":
        if prior_cash is None or type(prior_quantity) is not int or type(day_index) is not int:
            raise DomainError("M2_RULE_INPUT_REQUIRED", "Frozen account and owner window index required", 422)
        index = select_rule(definition, previous_close=previous_close, prior_cash=prior_cash,
                            prior_quantity=prior_quantity, day_index=day_index)
        if index is None:
            return Signal(action="no_change", reason="rules_no_match")
        return Signal(action="target_shares", target_shares=definition.rules[index].target_shares,
                      reason="rule_matched", rule_index=index)
    if value <= Decimal(definition.enter_below):
        return Signal(action="target_shares", target_shares=definition.target_shares, reason="enter_threshold")
    if value >= Decimal(definition.exit_above):
        return Signal(action="target_shares", target_shares=0, reason="exit_threshold")
    return Signal(action="no_change", reason="inside_band")
