"""Explicit synthetic full-session input, not market data or JQ evidence."""

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

SECURITIES = ["600000.XSHG", "000001.XSHE", "000002.XSHE"]


def fixture():
    days = ["2024-03-04", "2024-03-05", "2024-03-06"]
    doc = {
        "schema": "ad-minute-backtest.v1",
        "producer": {"name": "synthetic-example"},
        "real_market_data": False,
        "source_refs": ["hand-authored deterministic engineering fixture"],
        "limitations": ["Synthetic prices, states and calendar; no real market or JQ comparison."],
        "price_basis": "raw",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "visibility": "declared_available_at",
        "securities": SECURITIES,
        "calendar": ["2024-03-01", *days, "2024-03-07"],
        "days": days,
        "sessions": {},
        "states": [],
        "minute": [],
        "actions": [],
        "action_coverage": {
            "status": "synthetic_known_no_actions",
            "evidence": "fixture definition",
        },
    }
    previous = {s: Decimal(10 + n * 10) for n, s in enumerate(SECURITIES)}
    for di, day in enumerate(days):
        doc["sessions"][day] = [
            [f"{day}T09:30:00+08:00", f"{day}T11:30:00+08:00"],
            [f"{day}T13:00:00+08:00", f"{day}T15:00:00+08:00"],
        ]
        for si, sid in enumerate(SECURITIES):
            doc["states"].append(
                {
                    "security": sid,
                    "date": day,
                    "listed": True,
                    "suspended": False,
                    "is_st": False,
                    "lower_limit": "1",
                    "upper_limit": "100",
                    "previous_close": str(previous[sid]),
                    "available_at": f"{day}T08:00:00+08:00",
                    "evidence": "synthetic state",
                }
            )
            for n in range(240):
                session_start = datetime.fromisoformat(doc["sessions"][day][n // 120][0])
                start = session_start + timedelta(minutes=n % 120)
                end = start + timedelta(minutes=1)
                price = (
                    Decimal(10 + si * 10)
                    + Decimal(di) / 10
                    + Decimal(n * (1 if si != 1 else -1)) / 1000
                )
                doc["minute"].append(
                    {
                        "security": sid,
                        "bar_start": start.isoformat(),
                        "bar_end": end.isoformat(),
                        "available_at": end.isoformat(),
                        "open": str(price),
                        "high": str(price + Decimal("0.02")),
                        "low": str(price - Decimal("0.02")),
                        "close": str(price + Decimal("0.01")),
                        "volume": "10000",
                        "amount": str(price * 10000),
                        "evidence": f"synthetic:{di}:{si}:{n}",
                    }
                )
            previous[sid] = price + Decimal("0.01")
    return doc


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(fixture(), indent=2))
