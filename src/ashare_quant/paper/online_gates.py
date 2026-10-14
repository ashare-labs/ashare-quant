"""Forward observations are three separate arrivals, never one complete-day replay."""

from datetime import time
from decimal import Decimal

from ashare_quant.contracts import SHANGHAI

from .online_contracts import CONDITIONAL_MODEL
from .risk import close_gate, evidence_gate, indexed, require


def opening_gate(config, decision, opening):
    evidence_gate(config, opening)
    at = opening.received_at.astimezone(SHANGHAI)
    require(
        opening.trading_date == decision.trading_date
        and at.date() == opening.trading_date
        and time(9, 30) <= at.time() <= time(9, 35)
        and opening.received_at > decision.received_at,
        "PAPER_OPENING_WINDOW",
    )
    if config.model == CONDITIONAL_MODEL:
        proof = opening.execution_admission
        require(proof is not None, "PAPER_OPENING_ADMISSION_REQUIRED")
        require(proof.observed_at == opening.received_at, "PAPER_OPENING_ADMISSION_CLOCK")
        prior = indexed(decision.rows, config.universe)
        for sid, fact in indexed(proof.rows, config.universe).items():
            before = prior[sid]
            require(
                fact.eligible is True and fact.suspended is False and fact.is_st is False,
                "PAPER_OPENING_TRADING_STATE_BLOCKED",
            )
            require(
                Decimal(fact.reference_close) == Decimal(before.previous_close)
                and before.lower_limit is not None
                and before.upper_limit is not None
                and Decimal(fact.lower_limit) == Decimal(before.lower_limit)
                and Decimal(fact.upper_limit) == Decimal(before.upper_limit)
                and fact.rule_version == before.rule_version,
                "PAPER_OPENING_PARAMETERS_CHANGED",
            )
            if config.data_mode == "calendar_observed":
                old = (decision.strategy_receipt or {}).get("action_adjudications", {}).get(sid, {})
                baseline = (
                    old.get("action_scope", {})
                    .get("price_share_code_review", {})
                    .get("baseline_sha256")
                )
                require(fact.action_baseline_sha256 == baseline, "PAPER_OPENING_ACTION_BASELINE")
    else:
        require(opening.execution_admission is None, "PAPER_EXPLICIT_CONDITIONAL_MODEL_REQUIRED")
    for row in indexed(opening.rows, config.universe).values():
        source = row.source_as_of.astimezone(SHANGHAI)
        require(
            row.trading_date == opening.trading_date
            and source.date() == opening.trading_date
            and time(9, 30) <= source.time() <= time(9, 35)
            and row.source_as_of <= row.observed_at <= opening.received_at
            and Decimal(row.open) > 0,
            "PAPER_OPENING_NOT_OBSERVED",
        )


def closing_gate(config, decision, opening, close):
    close_gate(config, decision, close)
    require(close.intent_id == opening.intent_id, "PAPER_INTENT_MISMATCH")
    before = indexed(opening.rows, config.universe)
    for row in close.rows:
        require(
            Decimal(row.open) == Decimal(before[row.security].open),
            "PAPER_OPENING_PRICE_REVISION",
        )
