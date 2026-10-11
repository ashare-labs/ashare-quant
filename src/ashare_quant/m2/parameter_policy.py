"""Finite human-selected parameters; no data, matching or strategy-code authority."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from ashare_quant.contracts import Contract

from .common import decimal_text, require
from .contracts import Sha, StrategyDefinition

Cents = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$", max_length=12)]


class BandParameters(Contract):
    template: Literal["previous_close_band.v1"] = "previous_close_band.v1"
    security: Literal["600000.XSHG"] = "600000.XSHG"
    frequency: Literal["1d"] = "1d"
    initial_cash: Cents = "10000"
    target_shares: Literal[100, 200] = 100
    enter_below: Cents = "12.5"
    exit_above: Cents = "12.55"

    @field_validator("target_shares", mode="before")
    @classmethod
    def integer_lots(cls, value):
        if type(value) is not int:
            raise ValueError("target_shares must be an integer, either 100 or 200")
        return value

    @field_validator("initial_cash", "enter_below", "exit_above")
    @classmethod
    def canonical_decimal(cls, value):
        return decimal_text(value)

    @model_validator(mode="after")
    def bounds(self):
        if not Decimal("1000") <= Decimal(self.initial_cash) <= Decimal("10000"):
            raise ValueError("initial_cash must be between 1000 and 10000 inclusive")
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
    initial_cash_min: Literal["1000"] = "1000"
    initial_cash_max: Literal["10000"] = "10000"
    target_shares: tuple[Literal[100], Literal[200]] = (100, 200)
    maximum_decimal_places: Literal[2] = 2
    threshold_rule: Literal["0 < enter_below < exit_above <= 1000000"] = (
        "0 < enter_below < exit_above <= 1000000"
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
        if not Decimal("1000") <= Decimal(value) <= Decimal("10000"):
            raise ValueError("initial_cash must be between 1000 and 10000 inclusive")
        return decimal_text(value)


class RegisteredPythonParameters(RegisteredRuleParameters):
    template: Literal["registered_python.v1"] = "registered_python.v1"
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strategy_parameters", mode="before")
    @classmethod
    def flat_parameters(cls, value):
        from ashare_quant.python_strategies.spec import parameters

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
