"""Projection checks shared by current task receipts."""
import hashlib
import json
from decimal import Decimal


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError("Task result identity or provenance differs")


def _money(value):
    value = format(Decimal(str(value)), "f")
    return (value.rstrip("0").rstrip(".") if "." in value else value) or "0"


def _account(value):
    return dict(
        cash=value["cash"],
        equity=value["equity"],
        quantity=value["quantity"],
        sellable=value["sellable"],
        average_price=value["average"],
        dividend_receivable=value["receivable"],
        native_daily_transaction_cost=value["cost"],
    )


def _documents(packet):
    _require(set(packet) == {"query", "result", "sources", "evidence", "authorization"})
    return {k: json.loads(v) for k, v in packet.items()}
