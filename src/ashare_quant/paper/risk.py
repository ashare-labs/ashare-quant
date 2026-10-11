"""Strategy-independent input gates and read-only native reconciliation."""

from datetime import time
from decimal import Decimal, localcontext

from ashare_quant.contracts import SHANGHAI, DomainError
from ashare_quant.m2_jobs.files import digest

D = Decimal


def profiles():
    from .contracts import RiskPolicy

    return {
        "conservative_v1": RiskPolicy().model_dump(mode="json"),
        "target_then_native_cash_v1": RiskPolicy(
            profile="target_then_native_cash_v1",
            max_gross="0.98",
            max_single="0.34",
            cash_buffer="0",
            max_turnover="2",
            max_order_value="1000000",
            max_order_nav_ratio="0.40",
            execution_max_single="0.40",
        ).model_dump(mode="json"),
    }


def require(ok, code, message=None):
    if not ok:
        raise DomainError(code, message or code, 409)


def indexed(rows, universe):
    mapped = {r.security: r for r in rows}
    require(len(mapped) == len(rows) and set(mapped) == set(universe), "PAPER_SECURITY_SET")
    return mapped


def decision_gate(config, decision):
    events = {a.security: a for a in decision.cash_dividends}
    require(len(events) == len(decision.cash_dividends), "PAPER_ACTION_DUPLICATE")
    for a in events.values():
        require(
            config.cash_dividend_policy == "gross_zero_withholding_0900"
            and a.security in config.universe
            and a.record_date < a.ex_date <= decision.trading_date <= a.pay_date
            and (decision.trading_date != a.ex_date or a.record_date == decision.previous_open)
            and a.model_cash_available_at.isoformat() == str(a.pay_date) + "T09:00:00+08:00"
            and D(a.cash_per_share) > 0,
            "PAPER_ACTION_POLICY_MISMATCH",
        )
    evidence_gate(config, decision)
    if config.strategy_id:
        require(
            decision.strategy_receipt is not None
            and digest(decision.strategy_receipt) == decision.strategy_receipt_sha256,
            "PAPER_STRATEGY_RECEIPT_CHANGED",
        )
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
        if config.execution_admission_scope == "per_order":
            require(row.eligible is True, "PAPER_TRADING_STATE_BLOCKED")
        else:
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
        require(
            row.actions_known_absent
            or row.action_effect == "zero_entitlement"
            or row.action_effect == "native_cash_dividend"
            and row.security in events,
            "PAPER_ACTION_COVERAGE_UNSUPPORTED",
        )


def order_permission(config, decision, security, side):
    if config.execution_admission_scope != "per_order":
        return
    matches = [
        r
        for r in decision.order_admissions
        if r["row"]["security"] == security and r["report"]["admitted_side"] == side
    ]
    require(len(matches) == 1, "PAPER_AD_ORDER_SCOPE")
    permission = matches[0]
    report, admission, row = permission["report"], permission["admission"], permission["row"]
    day = decision.trading_date.isoformat()
    require(
        admission["ready"]
        and report["admission_scope"] == "single_order"
        and report["as_of"] == day + "T09:00:00+08:00"
        and row["date"] == day
        and admission["requests"] == [{"date": day, "security": security, "side": side}],
        "PAPER_AD_ORDER_SCOPE",
    )
    before = indexed(decision.rows, config.universe)[security]
    require(
        before.eligible is True and before.suspended is False and before.is_st is False,
        "PAPER_TRADING_STATE_BLOCKED",
    )
    limit_name = "upper_limit" if side == "BUY" else "lower_limit"
    require(
        getattr(before, limit_name) is not None
        and getattr(before, limit_name) == row[limit_name]["value"]
        and row["suspended"]["value"] is False
        and row["is_st"]["value"] is False,
        "PAPER_AD_ORDER_SCOPE",
    )


def close_gate(config, decision, close):
    evidence_gate(config, close)
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


def evidence_gate(config, value):
    historical = config.data_mode == "historical_research"
    require(
        value.evidence_kind == ("historical_research" if historical else "synthetic")
        and value.clock_basis == ("retrospective_model" if historical else "synthetic_clock"),
        "PAPER_EVIDENCE_MODE_MISMATCH",
    )


def pretrade(config, decision, account):
    with localcontext(prec=50):
        return _pretrade(config, decision, account)


def _pretrade(config, decision, account):
    if config.execution_admission_scope == "per_order":
        count = 0
        for security in config.universe:
            delta = decision.targets[security] - account["positions"][security]["quantity"]
            if delta:
                count += 1
                order_permission(config, decision, security, "BUY" if delta > 0 else "SELL")
        require(count == len(decision.order_admissions), "PAPER_AD_ORDER_SCOPE")
    if config.risk.profile == "target_then_native_cash_v1":
        return target_pretrade(config, decision, account)
    rows, risk = indexed(decision.rows, config.universe), config.risk
    nav, available = D(account["nav"]), D(account["available_cash"])
    require(nav > 0 and available >= 0, "PAPER_ACCOUNT_INVALID")
    gross = turnover = buys = D(0)
    holdings = 0
    for sid in config.universe:
        pos = account["positions"].get(sid, {"quantity": 0, "sellable": 0})
        target, qty = decision.targets[sid], pos["quantity"]
        pending = sum(
            o["remaining"]
            for o in account["open_orders"]
            if o["security"] == sid and o["side"] == "BUY"
        )
        if not max(qty, target) + pending:
            continue
        require(rows[sid].upper_limit is not None, "PAPER_TRADING_RULES_UNKNOWN")
        upper = D(rows[sid].upper_limit)
        exposure = (max(qty, target) + pending) * upper
        gross += exposure
        holdings += exposure > 0
        require(exposure <= nav * D(risk.max_single), "PAPER_CONCENTRATION_LIMIT")
        delta = target - qty
        require(delta >= 0 or -delta <= pos["sellable"], "PAPER_T1_LIMIT")
        require(delta % rows[sid].round_lot == 0 or delta < 0 and target == 0, "PAPER_LOT_LIMIT")
        notional = abs(delta) * upper
        limit(notional, D(risk.max_order_value), "PAPER_ORDER_VALUE_LIMIT", sid, "涨停价预算")
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


def limit(measured, cap, code, security=None, basis=""):
    require(measured <= cap, code, f"{security or '组合'}：{basis} {measured} > 上限 {cap} CNY")


def target_pretrade(config, decision, account):
    """Check the frozen target at its signal price, not two concurrent portfolios.

    Existing drift never prevents selling or an unchanged holding day. Execution
    still checks actual native cash, fees and exposure for every new buy.
    """
    rows, risk = indexed(decision.rows, config.universe), config.risk
    nav = D(account["nav"])
    require(nav > 0 and D(account["available_cash"]) >= 0, "PAPER_ACCOUNT_INVALID")
    gross = turnover = D(0)
    holdings = 0
    increasing = False
    for sid in config.universe:
        pos = account["positions"].get(sid, {"quantity": 0, "sellable": 0})
        target, qty, price = decision.targets[sid], pos["quantity"], D(rows[sid].previous_close)
        delta = target - qty
        require(delta >= 0 or -delta <= pos["sellable"], "PAPER_T1_LIMIT")
        require(delta % rows[sid].round_lot == 0 or delta < 0 and target == 0, "PAPER_LOT_LIMIT")
        gross += target * price
        turnover += abs(delta) * price
        holdings += target > 0
        if delta > 0:
            increasing = True
            limit(
                target * price,
                nav * D(risk.max_single),
                "PAPER_CONCENTRATION_LIMIT",
                sid,
                "前收目标市值",
            )
            limit(
                delta * price,
                min(D(risk.max_order_value), nav * D(risk.max_order_nav_ratio)),
                "PAPER_ORDER_VALUE_LIMIT",
                sid,
                "前收买单金额",
            )
    require(holdings <= risk.max_holdings, "PAPER_HOLDINGS_LIMIT")
    if increasing:
        limit(gross, nav * D(risk.max_gross), "PAPER_GROSS_LIMIT", basis="前收目标总市值")
    limit(turnover, nav * D(risk.max_turnover), "PAPER_TURNOVER_LIMIT", basis="双边前收换手金额")
    return {
        "status": "passed",
        "profile": risk.profile,
        "target_value": str(gross),
        "target_cap": str(nav * D(risk.max_gross)),
        "two_sided_turnover": str(turnover),
        "cash_admission": "per_order_native_cash_after_actual_fills",
        "existing_drift_does_not_block_reductions": True,
    }


def order_admission(config, account, security, delta, price):
    """Read-only assessment immediately before native submission; no shadow book."""
    with localcontext(prec=50):
        nav, price = D(account["nav"]), D(str(price))
        cash, cost = D(account["available_cash"]), abs(delta) * price
        risk = config.risk
        result = {
            "security": security,
            "quantity": delta,
            "price": str(price),
            "cash_before": str(cash),
            "nav_before": str(nav),
            "notional": str(cost),
            "status": "passed",
            "code": None,
            "message": "原生可用现金及风险检查通过",
        }
        pos = account["positions"].get(security, {"quantity": 0, "sellable": 0})
        if delta < 0:
            require(-delta <= pos["sellable"], "PAPER_T1_LIMIT")
            return result
        fee = max(D(config.minimum_commission), cost * D(config.commission_rate))
        cash_cap = cash - nav * D(risk.cash_buffer)
        pending = sum(
            o["remaining"]
            for o in account["open_orders"]
            if o["security"] == security and o["side"] == "BUY"
        )
        checks = [
            (
                cost,
                min(D(risk.max_order_value), nav * D(risk.max_order_nav_ratio)),
                "PAPER_ORDER_VALUE_LIMIT",
                "实际开盘买单金额",
            ),
            (
                (pos["quantity"] + pending + delta) * price,
                nav * D(risk.execution_max_single),
                "PAPER_CONCENTRATION_LIMIT",
                "执行后单股市值",
            ),
            (cost + fee, cash_cap, "PAPER_CASH_BUFFER", "买入金额与费用"),
        ]
        for measured, cap, code, name in checks:
            if measured > cap:
                result.update(
                    status="rejected",
                    code=code,
                    measured=str(measured),
                    cap=str(cap),
                    fee=str(fee),
                    message=f"{security}：{name} {measured} > 可用上限 {cap} CNY；拒绝本笔，保留已成交结果",
                )
                break
        return result


def reconcile(before, after, trades):
    with localcontext(prec=50):
        return _reconcile(before, after, trades)


def _reconcile(before, after, trades):
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
    require(abs(residual) <= D("0.000001"), "PAPER_CASH_RECONCILIATION")
    require(D(after["available_cash"]) >= 0 and D(after["frozen_cash"]) >= 0, "PAPER_NEGATIVE_CASH")
    value = D(after["total_cash"])
    for sid, pos in after["positions"].items():
        require(
            pos["quantity"] == quantities.get(sid, 0) and 0 <= pos["sellable"] <= pos["quantity"],
            "PAPER_POSITION_RECONCILIATION",
        )
        require(
            D(pos.get("dividend_receivable", "0"))
            == D(before["positions"][sid].get("dividend_receivable", "0")),
            "PAPER_INTRADAY_RECEIVABLE_CHANGED",
        )
        value += D(pos["market_value"]) + D(pos.get("dividend_receivable", "0"))
    require(abs(D(after["nav"]) - value) <= D("0.000001"), "PAPER_NAV_RECONCILIATION")
    return {"status": "passed", "cash_residual": str(residual), "fill_count": len(trades)}


def reconcile_opening(before, after, decision):
    """Read-only dividend audit against native positions; no posting authority."""
    with localcontext(prec=50):
        cash_delta, receipts = D(0), []
        for sid, pos in before["positions"].items():
            events = [a for a in decision.cash_dividends if a.security == sid]
            earned = paid = D(0)
            for a in events:
                amount = D(a.cash_per_share) * a.record_quantity
                if a.ex_date == decision.trading_date:
                    require(pos["quantity"] == a.record_quantity, "PAPER_ACTION_RECORD_QUANTITY")
                    earned += amount
                    if pos["quantity"]:
                        require(
                            abs(
                                D(after["positions"][sid]["average_cost"])
                                - (D(pos["average_cost"]) - D(a.cash_per_share))
                            )
                            <= D("0.000001"),
                            "PAPER_ACTION_COST_RECONCILIATION",
                        )
                if a.pay_date == decision.trading_date:
                    paid += amount
                receipts.append(
                    {
                        **a.model_dump(mode="json"),
                        "gross_entitlement": str(amount),
                        "receivable_added": str(
                            amount if a.ex_date == decision.trading_date else 0
                        ),
                        "cash_paid": str(amount if a.pay_date == decision.trading_date else 0),
                        "withholding": "0",
                        "classification": "read_only_reconciliation_of_native_posting",
                    }
                )
            actual = after["positions"][sid]
            require(actual["quantity"] == pos["quantity"], "PAPER_ACTION_POSITION_RECONCILIATION")
            require(
                abs(
                    D(actual.get("dividend_receivable", "0"))
                    - (D(pos.get("dividend_receivable", "0")) + earned - paid)
                )
                <= D("0.000001"),
                "PAPER_ACTION_RECEIVABLE_RECONCILIATION",
            )
            cash_delta += paid
        residual = D(after["total_cash"]) - D(before["total_cash"]) - cash_delta
        require(abs(residual) <= D("0.000001"), "PAPER_ACTION_CASH_RECONCILIATION")
        return {
            "status": "passed",
            "cash_delta": str(cash_delta),
            "residual": str(residual),
            "actions": receipts,
        }
