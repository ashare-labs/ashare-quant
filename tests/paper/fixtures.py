"""Explicit synthetic owner and fixed intents; not the shared real strategies."""

from decimal import Decimal

from ashare_quant.m2_jobs.files import digest
from ashare_quant.paper.contracts import SessionConfig

SECURITIES = ("000001.XSHE", "600000.XSHG")
DATES = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09")


def config(account="controlled-native-account"):
    return SessionConfig(
        account_id=account,
        strategy_version=digest("frozen-controlled-intents"),
        scenario_manifest_sha256=digest("synthetic-only-not-real-strategies"),
        initial_cash="10000",
        universe=SECURITIES,
    )


def decision(session, index, targets, parent=None):
    prior, day, successor = DATES[index : index + 3]
    rows = [
        {
            "security": sid,
            "price_date": prior,
            "previous_close": price,
            "observed_at": prior + "T16:00:00+08:00",
            "complete": True,
            "source_watermark": prior,
            "selected_watermark": prior,
            "eligible": True,
            "suspended": False,
            "is_st": False,
            "lower_limit": str(Decimal(price) * Decimal("0.9")),
            "upper_limit": str(Decimal(price) * Decimal("1.1")),
            "rule_version": "synthetic-rules-v1",
            "actions_known_absent": True,
            "facts_available_at": prior + "T16:00:00+08:00",
        }
        for sid, price in zip(SECURITIES, ("10", "20"), strict=True)
    ]
    return {
        "trading_date": day,
        "previous_open": prior,
        "next_open": successor,
        "logical_at": day + "T09:00:00+08:00",
        "received_at": day + "T09:00:01+08:00",
        "calendar_available_at": prior + "T16:00:00+08:00",
        "decision_deadline": day + "T09:05:00+08:00",
        "expected_parent": parent,
        "strategy_version": session.strategy_version,
        "strategy_receipt_sha256": digest(targets),
        "data_manifest_sha256": digest(rows),
        "rows": rows,
        "targets": dict(zip(SECURITIES, targets, strict=True)),
        "reason": "controlled native test",
    }


def close(day, intent_id, volume=100000):
    rows = [
        {
            "security": sid,
            "trading_date": day,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "observed_at": day + "T16:00:00+08:00",
            "complete": True,
            "source_watermark": day,
            "selected_watermark": day,
        }
        for sid, price in zip(SECURITIES, ("10", "20"), strict=True)
    ]
    return {
        "trading_date": day,
        "intent_id": intent_id,
        "data_manifest_sha256": digest(rows),
        "received_at": day + "T16:10:00+08:00",
        "deadline": day + "T18:00:00+08:00",
        "rows": rows,
    }


def queue(service, sid, session, index, targets, parent=None, volume=100000):
    request = decision(session, index, targets, parent)
    prepared = service.prepare(sid, request)
    service.seal_close(sid, close(prepared["day"], prepared["intent_id"], volume))
    return prepared["day"]
