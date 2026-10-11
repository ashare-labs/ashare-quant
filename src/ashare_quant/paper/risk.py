"""Strategy-independent input gates and read-only native reconciliation."""

from datetime import time
from decimal import Decimal

from ashare_quant.contracts import SHANGHAI, DomainError

D = Decimal


def require(ok, code, message=None):
    if not ok:
        raise DomainError(code, message or code, 409)


def indexed(rows, universe):
    mapped = {r.security: r for r in rows}
    require(len(mapped) == len(rows) and set(mapped) == set(universe), "PAPER_SECURITY_SET")
    return mapped


def decision_gate(config, decision):
    logical = decision.logical_at.astimezone(SHANGHAI)
    require(decision.strategy_version == config.strategy_version, "PAPER_STRATEGY_IDENTITY")
    require(set(decision.targets) == set(config.universe), "PAPER_TARGET_SET")
    require(decision.previous_open < decision.trading_date < decision.next_open, "PAPER_CALENDAR")
    require(
        logical.date() == decision.trading_date and logical.time() == time(9),
        "PAPER_DECISION_CLOCK",
    )
    require(
        decision.logical_at <= decision.received_at <= decision.decision_deadline
        and decision.decision_deadline.astimezone(SHANGHAI).date() == decision.trading_date
        and decision.decision_deadline.astimezone(SHANGHAI).time() <= time(9, 5),
        "PAPER_MISSED_DECISION",
    )
    require(decision.calendar_available_at <= decision.logical_at, "PAPER_CALENDAR_NOT_VISIBLE")
    for row in indexed(decision.rows, config.universe).values():
        require(
            row.complete
            and row.price_date == decision.previous_open
            and row.source_watermark == decision.previous_open
            and row.selected_watermark == decision.previous_open,
            "PAPER_DATA_STALE_OR_MISSING",
        )
        require(D(row.previous_close) > 0, "PAPER_PRICE_INVALID")
        require(
            row.observed_at <= decision.logical_at
            and row.facts_available_at <= decision.logical_at,
            "PAPER_DATA_NOT_VISIBLE",
        )
        require(
            row.eligible is True and row.suspended is False and row.is_st is False,
            "PAPER_TRADING_STATE_BLOCKED",
        )
        require(
            row.lower_limit is not None
            and row.upper_limit is not None
            and 0 < D(row.lower_limit) <= D(row.upper_limit)
            and bool(row.rule_version),
            "PAPER_TRADING_RULES_UNKNOWN",
        )
        require(row.actions_known_absent, "PAPER_ACTION_COVERAGE_UNSUPPORTED")


def close_gate(config, decision, close):
    require(close.trading_date == decision.trading_date, "PAPER_CLOSE_DATE")
    at = close.received_at.astimezone(SHANGHAI)
    require(
        at.date() == close.trading_date
        and at.time() >= time(15, 5)
        and close.received_at <= close.deadline
        and close.deadline.astimezone(SHANGHAI).date() == close.trading_date
        and close.deadline.astimezone(SHANGHAI).time() <= time(18),
        "PAPER_CLOSE_DEADLINE",
    )
    for row in indexed(close.rows, config.universe).values():
        require(
            row.trading_date == close.trading_date
            and row.complete
            and not row.conflict
            and row.volume > 0
            and row.source_watermark == close.trading_date
            and row.selected_watermark == close.trading_date,
            "PAPER_CLOSE_DATA_INCOMPLETE",
        )
        observed = row.observed_at.astimezone(SHANGHAI)
        require(
            observed.date() == close.trading_date
            and observed.time() >= time(15, 5)
            and row.observed_at <= close.received_at,
            "PAPER_CLOSE_NOT_OBSERVED",
        )


def pretrade(config, decision, account):
    rows, risk = indexed(decision.rows, config.universe), config.risk
    nav, available = D(account["nav"]), D(account["available_cash"])
    require(nav > 0 and available >= 0, "PAPER_ACCOUNT_INVALID")
    gross = turnover = buys = D(0)
    holdings = 0
    for sid in config.universe:
        pos = account["positions"].get(sid, {"quantity": 0, "sellable": 0})
        target, qty, upper = decision.targets[sid], pos["quantity"], D(rows[sid].upper_limit)
        pending = sum(
            o["remaining"]
            for o in account["open_orders"]
            if o["security"] == sid and o["side"] == "BUY"
        )
        exposure = (max(qty, target) + pending) * upper
        gross += exposure
        holdings += exposure > 0
        require(exposure <= nav * D(risk.max_single), "PAPER_CONCENTRATION_LIMIT")
        delta = target - qty
        require(delta >= 0 or -delta <= pos["sellable"], "PAPER_T1_LIMIT")
        require(delta % rows[sid].round_lot == 0 or delta < 0 and target == 0, "PAPER_LOT_LIMIT")
        notional = abs(delta) * upper
        require(notional <= D(risk.max_order_value), "PAPER_ORDER_VALUE_LIMIT")
        turnover += notional
        if delta > 0:
            buys += notional + max(
                D(config.minimum_commission), notional * D(config.commission_rate)
            )
    require(holdings <= risk.max_holdings, "PAPER_HOLDINGS_LIMIT")
    require(gross <= nav * D(risk.max_gross), "PAPER_GROSS_LIMIT")
    require(turnover <= nav * D(risk.max_turnover), "PAPER_TURNOVER_LIMIT")
    require(available - buys >= nav * D(risk.cash_buffer), "PAPER_CASH_BUFFER")
    return {
        "status": "passed",
        "worst_exposure": str(gross),
        "buy_budget": str(buys),
        "turnover": str(turnover),
    }


def reconcile(before, after, trades):
    expected_cash = D(before["total_cash"])
    quantities = {s: p["quantity"] for s, p in before["positions"].items()}
    seen = set()
    for trade in trades:
        require(trade["id"] not in seen, "PAPER_DUPLICATE_FILL")
        seen.add(trade["id"])
        sign = 1 if trade["side"] == "BUY" else -1
        expected_cash -= sign * D(trade["price"]) * trade["quantity"] + D(trade["cost"])
        quantities[trade["security"]] = (
            quantities.get(trade["security"], 0) + sign * trade["quantity"]
        )
    residual = D(after["total_cash"]) - expected_cash
    require(abs(residual) <= D("0.01"), "PAPER_CASH_RECONCILIATION")
    require(D(after["available_cash"]) >= 0 and D(after["frozen_cash"]) >= 0, "PAPER_NEGATIVE_CASH")
    value = D(after["total_cash"])
    for sid, pos in after["positions"].items():
        require(
            pos["quantity"] == quantities.get(sid, 0) and 0 <= pos["sellable"] <= pos["quantity"],
            "PAPER_POSITION_RECONCILIATION",
        )
        value += D(pos["market_value"])
    require(abs(D(after["nav"]) - value) <= D("0.01"), "PAPER_NAV_RECONCILIATION")
    return {"status": "passed", "cash_residual": str(residual), "fill_count": len(trades)}
