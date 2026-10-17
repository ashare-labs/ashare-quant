"""Bounded user-authored decisions, with no code evaluation, data IO or account writes."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ashare_quant.backtests.daily.quantity import MAX_EXACT_SHARES, TargetQuantity
from ashare_quant.contracts import Contract
from ashare_quant.storage.canonical import decimal_text

RuleField = Literal["previous_close", "prior_cash", "prior_quantity", "day_index"]
RuleOperator = Literal["lt", "le", "eq", "ne", "ge", "gt"]
RuleNumber = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$", max_length=24)]


class RuleCondition(Contract):
    field: RuleField
    operator: RuleOperator
    value: RuleNumber

    @field_validator("value")
    @classmethod
    def normalize(cls, value):
        return decimal_text(value)

    @model_validator(mode="after")
    def bounds(self):
        number = Decimal(self.value)
        if self.field in {"prior_quantity", "day_index"} and number != number.to_integral_value():
            raise ValueError("Share quantity and day index require integral constants")
        return self


class DecisionRule(Contract):
    rule_id: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]
    when_all: tuple[RuleCondition, ...] = Field(max_length=8)
    target_shares: TargetQuantity

    @field_validator("target_shares", mode="before")
    @classmethod
    def exact_lot(cls, value):
        if type(value) is not int:
            raise ValueError("Target must be an integer number of shares")
        return value


class RuleStrategyDefinition(Contract):
    template: Literal["decision_rules.v1"] = "decision_rules.v1"
    rules: tuple[DecisionRule, ...] = Field(min_length=1, max_length=16)
    otherwise: Literal["no_change"] = "no_change"

    @model_validator(mode="after")
    def ordering(self):
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule identifiers must be unique")
        if any(not r.when_all for r in self.rules[:-1]):
            raise ValueError("An unconditional rule must be last")
        return self


class RuleStrategyCapabilities(Contract):
    contract_version: Literal["m2-decision-rules.v1"] = "m2-decision-rules.v1"
    inputs: tuple[RuleField, ...] = ("previous_close", "prior_cash", "prior_quantity", "day_index")
    operators: tuple[RuleOperator, ...] = ("lt", "le", "eq", "ne", "ge", "gt")
    max_rules: Literal[16] = 16
    max_conditions_per_rule: Literal[8] = 8
    target_shares: tuple[Literal[0], Literal[100], Literal[200]] = (0, 100, 200)
    target_shares_are_examples: Literal[True] = True
    target_shares_min: Literal[0] = 0
    target_shares_multiple_of: Literal[100] = 100
    target_shares_max: int = MAX_EXACT_SHARES
    ordering: Literal["first_match_all_conditions"] = "first_match_all_conditions"
    otherwise: Literal["no_change"] = "no_change"
    required_data: Literal["owner_authorized_previous_close_each_day"] = (
        "owner_authorized_previous_close_each_day"
    )
    account_input: Literal["prior_committed_snapshot"] = "prior_committed_snapshot"
    day_index: Literal["zero_based_owner_window_index"] = "zero_based_owner_window_index"
    code_execution: Literal[False] = False
    mutable_strategy_state: Literal[False] = False


def select_rule(definition, *, previous_close, prior_cash, prior_quantity, day_index):
    """Return the first matching rule index; all inputs already carry frozen provenance."""
    values = {
        "previous_close": Decimal(previous_close),
        "prior_cash": Decimal(prior_cash),
        "prior_quantity": Decimal(prior_quantity),
        "day_index": Decimal(day_index),
    }
    for index, rule in enumerate(definition.rules):
        matched = True
        for condition in rule.when_all:
            left, right = values[condition.field], Decimal(condition.value)
            comparisons = {
                "lt": left < right,
                "le": left <= right,
                "eq": left == right,
                "ne": left != right,
                "ge": left >= right,
                "gt": left > right,
            }
            if not comparisons[condition.operator]:
                matched = False
                break
        if matched:
            return index
    return None
