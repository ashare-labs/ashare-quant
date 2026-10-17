"""An independent, finite continuous-session paper plan; no exchange orders."""

from datetime import date, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ashare_quant.contracts import SHANGHAI, Contract, Money
from ashare_quant.paper.contracts import Quantity, Security, Sha
from ashare_quant.paper.risk import require

UNIVERSE = ("600000.XSHG", "600036.XSHG", "601318.XSHG")
REBALANCE_MODEL = "declared-inventory-target-minute-partial-cancel.v3"


class DeclaredInventory(Contract):
    """Test starting inventory, never evidence of previously executed purchases."""

    kind: Literal["declared_test_inventory"] = "declared_test_inventory"
    as_of: date
    quantities: dict[Security, Quantity]
    price_basis: Literal["pinned_prior_daily_close"] = "pinned_prior_daily_close"
    prior_trades_claimed: Literal[False] = False


def continuous(at):
    t = at.astimezone(SHANGHAI).time()
    return time(9, 30) <= t < time(11, 30) or time(13) <= t < time(14, 57)


class IntradayPlan(Contract):
    schema_version: Literal[
        "paper-intraday-plan.v1", "paper-intraday-plan.v2", "paper-intraday-plan.v3"
    ] = "paper-intraday-plan.v1"
    model: Literal[
        "prior-close-intent-next-complete-minute.v1",
        "prior-close-intent-minute-partial-cancel.v2",
        "declared-inventory-target-minute-partial-cancel.v3",
    ] = "prior-close-intent-next-complete-minute.v1"
    day: date
    start_at: AwareDatetime
    intent_deadline: AwareDatetime
    execution_deadline: AwareDatetime
    universe: tuple[Security, ...] = UNIVERSE
    initial_cash: Money = "30000"
    strategy_id: Literal["equal-weight", "acceptance-rebalance"] = "equal-weight"
    admission_reader_sha256: Sha
    action_baseline_sha256: Sha
    daily_capture: Sha
    minute_completion_delay_seconds: Literal[60] = 60
    max_minute_participation: Money = "0.01"
    remainder_policy: Literal["cancel_after_minute"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    buy_slippage: Literal["0.01"] = "0.01"
    initial_inventory: DeclaredInventory | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    target_quantities: dict[Security, Quantity] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def bounded(self):
        rebalance = self.model == REBALANCE_MODEL
        partial = self.model == "prior-close-intent-minute-partial-cancel.v2" or rebalance
        if rebalance:
            if not (
                self.strategy_id == "acceptance-rebalance"
                and self.initial_inventory is not None
                and self.initial_inventory.as_of < self.day
                and set(self.initial_inventory.quantities) == set(self.universe)
                and self.target_quantities is not None
                and set(self.target_quantities) == set(self.universe)
                and all(
                    q % 100 == 0
                    for q in (
                        *self.initial_inventory.quantities.values(),
                        *self.target_quantities.values(),
                    )
                )
            ):
                raise ValueError(
                    "Explicit prior inventory and whole-lot acceptance targets required"
                )
        elif (
            self.strategy_id != "equal-weight"
            or self.initial_inventory is not None
            or self.target_quantities is not None
        ):
            raise ValueError("Original strategy and empty genesis are immutable")
        if partial:
            if not (
                self.schema_version
                == ("paper-intraday-plan.v3" if rebalance else "paper-intraday-plan.v2")
                and self.remainder_policy == "cancel_after_minute"
                and 0 < Decimal(self.max_minute_participation) <= 1
            ):
                raise ValueError(
                    "Explicit partial-fill capacity and remainder cancellation required"
                )
        elif not (
            self.schema_version == "paper-intraday-plan.v1"
            and self.remainder_policy is None
            and self.max_minute_participation == "0.01"
        ):
            raise ValueError("Original strict-volume model is immutable")
        times = (self.start_at, self.intent_deadline, self.execution_deadline)
        if not (
            self.start_at < self.intent_deadline < self.execution_deadline
            and self.execution_deadline - self.start_at <= timedelta(minutes=15)
            and all(t.astimezone(SHANGHAI).date() == self.day and continuous(t) for t in times)
            and len({t.astimezone(SHANGHAI).hour < 12 for t in times}) == 1
            and self.universe == UNIVERSE
            and Decimal(self.initial_cash) == 30000
        ):
            raise ValueError(
                "Finite 30000 cash, exact three securities, one continuous session required"
            )
        return self


class IntradayMinute(Contract):
    security: Security
    minute_end: AwareDatetime
    raw_close: Money
    match_price: Money
    volume: int = Field(strict=True, gt=0)
    source_sha256: Sha
    confirmation_sha256: Sha
    observed_at: AwareDatetime


class IntradayProof(Contract):
    plan_sha256: Sha
    intent_received_at: AwareDatetime
    rows: tuple[IntradayMinute, ...] = Field(min_length=3, max_length=3)


def plan_for(config, decision):
    from ashare_quant.storage.artifacts import digest

    p = IntradayPlan.model_validate((decision.strategy_receipt or {}).get("intraday_plan"))
    continuation = (decision.strategy_receipt or {}).get("continuation")
    if continuation is not None:
        from ashare_quant.paper.intraday_continuation import manifest_gate

        m = manifest_gate(continuation, config, decision)
        require(p == m.plan, "PAPER_CONTINUATION_PLAN_CHANGED")
        return p
    require(
        digest(p) == config.scenario_manifest_sha256
        and p.model == config.model
        and p.initial_cash == config.initial_cash
        and p.universe == config.universe
        and p.admission_reader_sha256 == config.admission_reader_sha256,
        "PAPER_INTRADAY_PLAN_IDENTITY",
    )
    return p


def decision_gate(config, decision):
    p = plan_for(config, decision)
    if p.model == REBALANCE_MODEL:
        from ashare_quant.storage.artifacts import digest

        require(
            decision.targets == p.target_quantities
            and decision.previous_open == p.initial_inventory.as_of
            and decision.expected_parent is None
            and digest(decision.strategy_receipt) == decision.strategy_receipt_sha256,
            "PAPER_REBALANCE_DECLARATION_CHANGED",
        )
    require(
        decision.trading_date == p.day
        and p.start_at <= decision.logical_at <= decision.received_at <= p.intent_deadline
        and decision.decision_deadline == p.intent_deadline
        and continuous(decision.received_at),
        "PAPER_INTRADAY_INTENT_WINDOW",
    )


def execution_gate(config, decision, opening):
    from ashare_quant.paper.risk import indexed
    from ashare_quant.storage.artifacts import digest

    p = plan_for(config, decision)
    proof = opening.intraday
    require(
        proof is not None
        and proof.plan_sha256 == digest(p)
        and proof.intent_received_at == decision.received_at,
        "PAPER_INTRADAY_EXECUTION_PROOF",
    )
    require(
        decision.received_at < opening.received_at <= p.execution_deadline
        and continuous(opening.received_at),
        "PAPER_INTRADAY_MATCH_WINDOW",
    )
    require(len({r.minute_end for r in proof.rows}) == 1, "PAPER_INTRADAY_MINUTE_SET")
    prices = indexed(opening.rows, config.universe)
    for sid, row in indexed(proof.rows, config.universe).items():
        require(
            row.minute_end - timedelta(minutes=1) > decision.received_at
            and row.minute_end + timedelta(seconds=60) <= row.observed_at <= opening.received_at
            and (opening.received_at - row.minute_end).total_seconds() <= 180
            and continuous(row.minute_end),
            "PAPER_INTRADAY_MINUTE_CAUSALITY",
        )
        delta = (
            decision.targets[sid] - p.initial_inventory.quantities[sid]
            if p.model == REBALANCE_MODEL
            else 1
        )
        tick = Decimal(p.buy_slippage) * (1 if delta > 0 else -1 if delta < 0 else 0)
        require(
            Decimal(row.match_price) == Decimal(row.raw_close) + tick
            and Decimal(prices[sid].open) == Decimal(row.match_price)
            and prices[sid].raw_sha256 == row.source_sha256
            and prices[sid].source_as_of == row.minute_end,
            "PAPER_INTRADAY_PRICE_PROOF",
        )
        if p.model == "prior-close-intent-next-complete-minute.v1":
            require(
                decision.targets[sid]
                <= int(Decimal(row.volume) * Decimal(p.max_minute_participation)),
                "PAPER_INTRADAY_VOLUME_LIMIT",
            )
