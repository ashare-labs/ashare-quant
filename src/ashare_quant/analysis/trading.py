"""Cash-equity trade attribution from frozen fills, with conservative failure states.

Closed cycles run from zero shares to zero shares. Realized fill PnL uses a moving
average cost including buy costs; dividends are attributed separately. Share
corporate actions and incomplete cash settlements are explicitly unsupported.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .policy import AnalysisInputError, metric, number, session


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AnalysisInputError("invalid_trade_record")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AnalysisInputError("invalid_trade_record") from exc
    if not result.is_finite() or abs(result) > Decimal("1e24"):
        raise AnalysisInputError("invalid_trade_record")
    return result


def analyze_trading(native, daily, initial_equity):
    raw = native.get("trades")
    days = {row["date"] for row in daily}
    flows = {day: {"buy_value": None, "sell_value": None} for day in days}
    unavailable = {"status": "unavailable", "reason": "missing_trades", "closed_trades": [],
                   "fill_pnl": [], "positions": []}
    costs = {"status": "unavailable", "reason": "missing_trades", "commission": None,
             "tax": None, "total": None, "dividend_cash": None}
    if not isinstance(raw, list):
        return unavailable, costs, flows, _metrics([], "missing_trades")
    try:
        trades, ids, prior = [], set(), None
        for row in raw:
            day = session(row.get("at"))
            stamp = datetime.fromisoformat(row["at"])
            trade_id, security, side = row.get("trade_id"), row.get("security"), row.get("side")
            if (day not in days or not isinstance(trade_id, str) or trade_id in ids
                    or not isinstance(security, str) or not security or side not in {"BUY", "SELL"}
                    or (prior is not None and stamp < prior)):
                raise AnalysisInputError("invalid_trade_record")
            qty, price, commission, tax = [_decimal(row.get(key))
                                           for key in ("amount", "price", "commission", "tax")]
            if qty <= 0 or qty != int(qty) or price <= 0 or min(commission, tax) < 0:
                raise AnalysisInputError("invalid_trade_record")
            ids.add(trade_id)
            prior = stamp
            trades.append(dict(row, date=day, qty=qty, price=price, commission=commission, tax=tax))
    except (AnalysisInputError, AttributeError, TypeError) as exc:
        reason = exc.code if isinstance(exc, AnalysisInputError) else "invalid_trade_record"
        return dict(unavailable, reason=reason), dict(costs, reason=reason), flows, _metrics([], reason)
    flows = {day: {"buy_value": 0.0, "sell_value": 0.0} for day in days}
    for row in trades:
        flows[row["date"]]["buy_value" if row["side"] == "BUY" else "sell_value"] += float(
            row["qty"] * row["price"])
    commission = sum((row["commission"] for row in trades), Decimal(0))
    tax = sum((row["tax"] for row in trades), Decimal(0))
    costs = dict(status="ok", reason=None, commission=float(commission), tax=float(tax),
                 total=float(commission + tax), dividend_cash=0.0,
                 dividend_status="ok", dividend_reason=None)
    try:
        distributions = _distributions(native)
        costs["dividend_cash"] = float(sum((item["cash"] for item in distributions), Decimal(0)))
        trading = _attribute(trades, daily, distributions, initial_equity)
    except AnalysisInputError as exc:
        trading = dict(unavailable, reason=exc.code)
        if exc.code.startswith("corporate_") or exc.code == "invalid_trade_record":
            costs.update(dividend_cash=None, dividend_status="unavailable", dividend_reason=exc.code)
        return trading, costs, flows, _metrics([], exc.code)
    return trading, costs, flows, _metrics(trading["closed_trades"], None)


def _distributions(native):
    entitlements = native.get("corporate_entitlements", {})
    if not isinstance(entitlements, dict):
        raise AnalysisInputError("corporate_records_incomplete")
    result = []
    for security, items in entitlements.items():
        if not isinstance(items, dict):
            raise AnalysisInputError("corporate_records_incomplete")
        for item in items.values():
            if not isinstance(item, dict):
                raise AnalysisInputError("corporate_records_incomplete")
            if _decimal(item.get("shares")) != 0:
                raise AnalysisInputError("corporate_share_adjustment")
            cash = _decimal(item.get("cash"))
            if cash < 0:
                raise AnalysisInputError("corporate_records_incomplete")
            if cash == 0:
                continue
            model = native.get("corporate_account_model", {})
            if model.get("tax") != "gross_cash_no_tax":
                raise AnalysisInputError("corporate_tax_model_unsupported")
            stages, event = item.get("stages", {}), item.get("event", {})
            if not isinstance(stages, dict) or not any(key.endswith(":cash") for key in stages):
                raise AnalysisInputError("corporate_cash_unsettled")
            cash_stages = [value for key, value in stages.items() if key.endswith(":cash")]
            if len(cash_stages) != 1:
                raise AnalysisInputError("corporate_records_incomplete")
            result.append({"security": security, "cash": cash,
                           "record_date": session(event.get("record_date")),
                           "paid_date": session(cash_stages[0]),
                           "basis": _decimal(item.get("basis"))})
    return result


def _attribute(trades, daily, distributions, initial_equity):
    held, closed, fill_pnl, positions = {}, [], [], []
    cash = _decimal(initial_equity)
    by_day = defaultdict(list)
    for row in trades:
        by_day[row["date"]].append(row)
    by_record = defaultdict(list)
    paid = defaultdict(lambda: Decimal(0))
    for item in distributions:
        by_record[item["record_date"]].append(item)
        paid[item["paid_date"]] += item["cash"]
    assigned = 0
    for day in daily:
        for row in by_day[day["date"]]:
            sid, qty = row["security"], row["qty"]
            state = held.get(sid)
            if row["side"] == "BUY":
                if state is None:
                    cycle = {"cycle_id": row["trade_id"], "security": sid,
                             "opened_at": row["at"], "closed_at": None,
                             "realized_pnl": Decimal(0), "dividend": Decimal(0),
                             "cash_pnl": Decimal(0)}
                    state = held[sid] = {"qty": Decimal(0), "cost": Decimal(0), "cycle": cycle}
                state["qty"] += qty
                state["cost"] += qty * row["price"] + row["commission"] + row["tax"]
                cash -= qty * row["price"] + row["commission"] + row["tax"]
                state["cycle"]["cash_pnl"] -= qty * row["price"] + row["commission"] + row["tax"]
                fill_pnl.append({"trade_id": row["trade_id"], "value": None,
                                 "reason": "not_a_closing_fill"})
            else:
                if state is None or qty > state["qty"]:
                    raise AnalysisInputError("unmatched_position_history")
                basis = state["cost"] * qty / state["qty"]
                pnl = qty * row["price"] - row["commission"] - row["tax"] - basis
                cash += qty * row["price"] - row["commission"] - row["tax"]
                state["cycle"]["cash_pnl"] += qty * row["price"] - row["commission"] - row["tax"]
                state["qty"] -= qty
                state["cost"] -= basis
                state["cycle"]["realized_pnl"] += pnl
                fill_pnl.append({"trade_id": row["trade_id"], "value": float(pnl), "reason": None})
                if state["qty"] == 0:
                    state["cycle"]["closed_at"] = row["at"]
                    closed.append(state["cycle"])
                    del held[sid]
        for item in by_record[day["date"]]:
            state = held.get(item["security"])
            if state is None or state["qty"] != item["basis"]:
                raise AnalysisInputError("corporate_record_basis_mismatch")
            state["cycle"]["dividend"] += item["cash"]
            assigned += 1
        cash += paid[day["date"]]
        if trades:
            actual_cash = day["cash"]
            locked = day["locked_cash"]
            if actual_cash is None or locked is None:
                raise AnalysisInputError("missing_account_cash")
            if abs(cash - Decimal(str(actual_cash)) - Decimal(str(locked))) > Decimal("0.00001"):
                raise AnalysisInputError("cash_ledger_mismatch")
        raw_positions = day.get("positions")
        if not isinstance(raw_positions, dict):
            raise AnalysisInputError("missing_position_snapshots")
        actual = {}
        for sid, pos in raw_positions.items():
            qty, value = number(pos.get("total_amount")), number(pos.get("value"))
            if qty is None or value is None:
                raise AnalysisInputError("invalid_position_snapshot")
            if qty:
                actual[sid] = qty
                state = held.get(sid)
                if state is None or state["qty"] != Decimal(str(qty)):
                    raise AnalysisInputError("unmatched_position_history")
                positions.append({"date": day["date"], "security": sid,
                                  "cost_including_buy_fees": float(state["cost"]),
                                  "unrealized_pnl": float(Decimal(str(value)) - state["cost"]),
                                  "weight": value / day["equity"] if day["equity"] > 0 else None})
        if set(actual) != set(held):
            raise AnalysisInputError("unmatched_position_history")
    if assigned != len(distributions):
        raise AnalysisInputError("corporate_record_date_outside_result")
    if any(item["paid_date"] not in {row["date"] for row in daily} for item in distributions):
        raise AnalysisInputError("corporate_payment_outside_result")
    output = []
    for cycle in closed:
        cash_pnl = cycle.pop("cash_pnl")
        output.append({**cycle, "realized_pnl": float(cash_pnl),
                       "dividend": float(cycle["dividend"]),
                       "net_pnl": float(cash_pnl + cycle["dividend"])})
    return {"status": "ok", "reason": None, "closed_trades": output,
            "open_cycles": len(held), "fill_pnl": fill_pnl, "positions": positions,
            "definition": "flat_to_flat; moving_average_including_buy_fees; cash_dividend_separate"}


def _metrics(closed, reason):
    n = len(closed)
    values = [item["net_pnl"] for item in closed]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    count_reason = reason
    ratio_reason = reason or ("no_closed_trades" if not n else None)
    payoff_reason = ratio_reason or ("no_winning_trades" if not wins else
                                    "no_losing_trades" if not losses else None)
    return {
        "winning_trades": metric(None if reason else len(wins), unit="count", reason=count_reason, count=n),
        "losing_trades": metric(None if reason else len(losses), unit="count", reason=count_reason, count=n),
        "win_rate": metric(len(wins) / n if n and not reason else None,
                           unit="percent", reason=ratio_reason, count=n),
        "profit_loss_ratio": metric((sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
                                    if wins and losses and not reason else None,
                                    reason=payoff_reason, count=n),
    }
