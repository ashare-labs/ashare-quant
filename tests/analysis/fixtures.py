"""Synthetic fixture for browser contract tests, never a strategy or market dataset."""

import hashlib
import json
import math
from datetime import date, timedelta

from ashare_quant.analysis import AnalysisPolicy, analyze_result


def browser_fixture(*, with_benchmark=True, sessions=70, job_number=1):
    days = []
    day = date(2025, 1, 2)
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    fills = {0: ("BUY", 2000, 10.0), 12: ("SELL", 800, 10.8), 25: ("SELL", 1200, 9.8),
             30: ("BUY", 2000, 12.0), 58: ("SELL", 2000, 13.5), 63: ("BUY", 1000, 13.0)}
    accounts, trades, orders = [], [], []
    cash, qty, avg = 100000.0, 0, 0.0
    for i, day in enumerate(days):
        if i in fills:
            side, amount, price = fills[i]
            commission, tax = 5.0, amount * price * .001 if side == "SELL" else 0.0
            if side == "BUY":
                avg = (avg * qty + amount * price) / (qty + amount)
                qty += amount
                cash -= amount * price + commission + tax
            else:
                qty -= amount
                cash += amount * price - commission - tax
            key = str(i + 1)
            trades.append({"trade_id": key, "order_id": key, "security": "SYNTHETIC_STOCK",
                           "at": day + "T10:00:00+08:00", "side": side, "amount": amount,
                           "price": price, "commission": commission, "tax": tax})
            orders.append({"order_id": key, "security": "SYNTHETIC_STOCK",
                           "add_time": day + "T09:59:00", "order_type": "MARKET"})
        if i == 11:
            cash += 400
        price = 10 + .04 * i + math.sin(i / 4) * .9
        accounts.append({"at": day + "T15:30:00+08:00", "available_cash": cash,
                         "locked_cash": 0, "positions_value": qty * price,
                         "total_value": cash + qty * price,
                         "positions": {"SYNTHETIC_STOCK": {"total_amount": qty,
                             "closeable_amount": 0 if i in fills and fills[i][0] == "BUY" else qty,
                             "price": price, "value": qty * price, "avg_cost": avg}} if qty else {}})
    native = {"accounts": accounts, "trades": trades, "orders": orders,
              "preflight": {"frequency": "1m"}, "historical_pit_verified": False,
              "corporate_account_model": {"tax": "gross_cash_no_tax"},
              "corporate_entitlements": {}}
    if sessions > 11:
        native["corporate_entitlements"] = {"SYNTHETIC_STOCK": {"synthetic-dividend": {
            "basis": 2000, "cash": "400", "shares": 0,
            "event": {"record_date": days[8]}, "stages": {"synthetic:cash": days[11]}}}}
    job_id = "m2job-" + str(job_number).zfill(32)
    values = [100000, *[account["total_value"] for account in accounts]]
    drawdown = max(1 - value / max(values[:i + 1]) for i, value in enumerate(values))
    result = {"job_id": job_id, "name": "界面测试（合成数据）", "summary": {
        "initial_cash": 100000, "final_equity": values[-1], "net_return": values[-1] / 100000 - 1,
        "net_change": values[-1] - 100000, "max_drawdown": drawdown,
        "trade_count": len(trades), "order_count": len(orders), "drawdown_sampling": "end_of_day"},
        "preparation": {"trading_dates": days}, "native": native, "logs": [], "release": {}, "parameters": {}}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    benchmark = {"security": "SYNTHETIC_INDEX", "name": "合成测试基准", "return_type": "price",
                 "source_sha256": "1" * 64, "initial_date": "2024-12-31", "initial_value": 1000,
                 "points": [{"date": day, "value": 1000 * (1.0003 ** (i + 1)) * (1 + .002 * math.sin(i))}
                            for i, day in enumerate(days)]}
    analysis = analyze_result(result, source_sha256=digest,
                              policy=AnalysisPolicy(annualization=252, risk_free_annual=0.0,
                                                    risk_free_source="assumed_zero", min_risk_observations=20),
                              benchmark=benchmark if with_benchmark else None)
    job = {"job_id": job_id, "request": {"name": result["name"], "frequency": "1m",
            "start_date": days[0], "end_date": days[-1], "initial_cash": 100000},
           "status": "succeeded", "created_at": "2025-05-01T00:00:00Z", "updated_at": "2025-05-01T00:00:00Z",
           "result": {"snapshot_sha256": digest}, "error": None, "can_resume": False}
    return {"job": job, "result": result, "analysis": analysis}
