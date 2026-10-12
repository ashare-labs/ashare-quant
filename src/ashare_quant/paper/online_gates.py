"""Forward observations are three separate arrivals, never one complete-day replay."""

from datetime import time
from decimal import Decimal

from ashare_quant.contracts import SHANGHAI

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
