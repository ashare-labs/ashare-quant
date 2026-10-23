"""Finite human-selected parameters; no data, matching or strategy-code authority."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from ashare_quant.backtests.daily.quantity import MAX_EXACT_SHARES, TargetQuantity
from ashare_quant.contracts import Contract
from ashare_quant.storage.canonical import decimal_text, require
from tests.support.legacy_backtests.contracts import Sha, StrategyDefinition

Cents = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$", max_length=12)]


class BandParameters(Contract):
    template: Literal["previous_close_band.v1"] = "previous_close_band.v1"
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["1d"] = "1d"
    initial_cash: Cents = "10000"
    target_shares: TargetQuantity = Field(default=100, ge=100)
    enter_below: Cents = "12.5"
    exit_above: Cents = "12.55"

    @field_validator("target_shares", mode="before")
    @classmethod
    def integer_lots(cls, value):
        if type(value) is not int:
            raise ValueError("target_shares must be an integer number of shares")
        return value

    @field_validator("initial_cash", "enter_below", "exit_above")
    @classmethod
    def canonical_decimal(cls, value):
        return decimal_text(value)

    @model_validator(mode="after")
    def bounds(self):
        if Decimal(self.initial_cash) <= 0:
            raise ValueError("initial_cash must be positive")
        self.definition()  # Reuse the established positive, strictly ordered threshold bounds.
        return self

    def definition(self):
        return StrategyDefinition(
            template=self.template,
            target_shares=self.target_shares,
            enter_below=self.enter_below,
            exit_above=self.exit_above,
        )


class ParameterLimits(Contract):
    policy_version: Literal["m2-band-parameters.v1"] = "m2-band-parameters.v1"
    initial_cash_min: Literal["0.01"] = "0.01"
    initial_cash_max: None = None
    initial_cash_max_text_length: Literal[12] = 12
    target_shares: tuple[Literal[100], Literal[200]] = (100, 200)
    target_shares_are_examples: Literal[True] = True
    target_shares_min: Literal[100] = 100
    target_shares_multiple_of: Literal[100] = 100
    target_shares_max: int = MAX_EXACT_SHARES
    maximum_decimal_places: Literal[2] = 2
    threshold_rule: Literal["0 < enter_below < exit_above"] = (
        "0 < enter_below < exit_above"
    )
    entry_comparison: Literal["previous_close <= enter_below"] = "previous_close <= enter_below"
    exit_comparison: Literal["previous_close >= exit_above"] = "previous_close >= exit_above"
    initial_position: Literal[0] = 0


class RegisteredRuleParameters(Contract):
    template: Literal["registered_rules.v1"] = "registered_rules.v1"
    strategy_version_id: Sha
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["1d"] = "1d"
    initial_cash: Cents = "10000"

    @field_validator("initial_cash")
    @classmethod
    def cash(cls, value):
        if Decimal(value) <= 0:
            raise ValueError("initial_cash must be positive")
        return decimal_text(value)


class RegisteredPythonParameters(RegisteredRuleParameters):
    template: Literal["registered_python.v1"] = "registered_python.v1"
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strategy_parameters", mode="before")
    @classmethod
    def flat_parameters(cls, value):
        from tests.support.legacy_python.spec import parameters

        return parameters(value)


JobParameters = Annotated[
    BandParameters | RegisteredRuleParameters | RegisteredPythonParameters,
    Field(discriminator="template"),
]


def parse_parameters(value):
    return TypeAdapter(JobParameters).validate_python(value)


def check_strategy(parameters, strategy):
    if isinstance(parameters, RegisteredPythonParameters):
        require(
            strategy.version_id == parameters.strategy_version_id
            and strategy.definition.template == "trusted_python_decision.v1",
            "M2_JOB_STRATEGY_CHANGED",
            "Use the exact registered Python version",
        )
    elif isinstance(parameters, RegisteredRuleParameters):
        require(
            strategy.version_id == parameters.strategy_version_id
            and strategy.definition.template == "decision_rules.v1",
            "M2_JOB_STRATEGY_CHANGED",
            "Use the exact registered rules version",
        )
    else:
        require(
            strategy.definition == parameters.definition(),
            "M2_JOB_STRATEGY_CHANGED",
            "Template parameters differ from the frozen version",
        )
    return strategy
