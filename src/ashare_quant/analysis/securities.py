"""Security-level PnL attribution and total account ledger reconciliation.

Pure price spreads exclude trading fees so that each security's net PnL satisfies:
    net_pnl = realized_spread + unrealized_spread + dividend_cash - fees
Securities with bonus/capitalization share adjustments retain exact ledger net PnL
and return contribution while leaving the unsplit price spreads None. Any mismatch
against account cash or ending equity fails closed without a plug/residual item.
"""

import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from .policy import AnalysisInputError, number, session


def _float_noise_tolerance(*amounts: Decimal) -> Decimal:
    scale = max((abs(v) for v in amounts), default=Decimal(1))
    ulp = Decimal(str(math.ulp(float(max(scale, Decimal(1))))))
    # Absorb IEEE-754 float64 representation noise only (~64 ULPs, floor 1e-5 CNY),
    # capped at 0.0005 CNY (0.05 cent) so any displayable cent gap fails closed.
    return min(Decimal("0.0005"), max(Decimal("0.00001"), ulp * 64))


def _decimal(value, code="invalid_trade_record"):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AnalysisInputError(code)
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AnalysisInputError(code) from exc
    if not result.is_finite() or abs(result) > Decimal("1e24"):
        raise AnalysisInputError(code)
    return result


def _stage_day(stages, suffix):
    matches = [value for key, value in stages.items()
               if isinstance(key, str) and key.endswith(":" + suffix)]
    if len(matches) > 1:
        raise AnalysisInputError("corporate_records_incomplete")
    if not matches:
        return None
    try:
        return session(matches[0])
    except AnalysisInputError as exc:
        raise AnalysisInputError("corporate_records_incomplete") from exc


def _optional_day(value, code):
    try:
        return session(value)
    except AnalysisInputError as exc:
        raise AnalysisInputError(code) from exc


def _parse_external_cash_flows(raw, day_set):
    if raw is None or raw == []:
        return False, Decimal(0), defaultdict(lambda: Decimal(0)), True
    if not isinstance(raw, list):
        raise AnalysisInputError("invalid_external_cash_flow")
    total = Decimal(0)
    by_day = defaultdict(lambda: Decimal(0))
    all_dated = True
    for item in raw:
        if not isinstance(item, dict) or "amount" not in item:
            raise AnalysisInputError("invalid_external_cash_flow")
        amount = _decimal(item.get("amount"), "invalid_external_cash_flow")
        total += amount
        stamp = item.get("date") if item.get("date") is not None else item.get("at")
        if stamp is not None:
            day = _optional_day(stamp, "invalid_external_cash_flow")
            if day not in day_set:
                raise AnalysisInputError("invalid_external_cash_flow")
            by_day[day] += amount
        else:
            all_dated = False
    return True, total, by_day, all_dated


def _parse_cash_interest(raw, day_set):
    by_day = defaultdict(lambda: Decimal(0))
    if raw is None or raw == [] or raw == {}:
        return Decimal(0), by_day, True
    if isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
        total = _decimal(raw, "invalid_cash_interest")
        return total, by_day, total == 0
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            total, by_day, all_dated = _parse_cash_interest(raw["items"], day_set)
            if "total" in raw and raw["total"] is not None:
                declared = _decimal(raw["total"], "invalid_cash_interest")
                if abs(declared - total) > Decimal("0.0001"):
                    raise AnalysisInputError("invalid_cash_interest")
            return total, by_day, all_dated
        raw_val = raw.get("total") if "total" in raw else raw.get("amount")
        total = _decimal(raw_val, "invalid_cash_interest")
        stamp = raw.get("date") if raw.get("date") is not None else raw.get("at")
        if stamp is not None:
            day = _optional_day(stamp, "invalid_cash_interest")
            if day not in day_set:
                raise AnalysisInputError("invalid_cash_interest")
            by_day[day] += total
            return total, by_day, True
        return total, by_day, total == 0
    if isinstance(raw, list):
        total = Decimal(0)
        all_dated = True
        for item in raw:
            if not isinstance(item, dict):
                raise AnalysisInputError("invalid_cash_interest")
            amount = _decimal(item.get("amount"), "invalid_cash_interest")
            total += amount
            stamp = item.get("date") if item.get("date") is not None else item.get("at")
            if stamp is not None:
                day = _optional_day(stamp, "invalid_cash_interest")
                if day not in day_set:
                    raise AnalysisInputError("invalid_cash_interest")
                by_day[day] += amount
            else:
                all_dated = False
        return total, by_day, all_dated
    raise AnalysisInputError("invalid_cash_interest")


def _parse_trades(raw, day_set):
    if not isinstance(raw, list):
        raise AnalysisInputError("missing_trades")
    trades, ids, prior = [], set(), None
    for row in raw:
        if not isinstance(row, dict):
            raise AnalysisInputError("invalid_trade_record")
        try:
            day = session(row.get("at"))
            stamp = datetime.fromisoformat(row["at"])
        except (AnalysisInputError, KeyError, TypeError, ValueError) as exc:
            raise AnalysisInputError("invalid_trade_record") from exc
        trade_id, security, side = row.get("trade_id"), row.get("security"), row.get("side")
        if (day not in day_set or not isinstance(trade_id, str) or not trade_id or trade_id in ids
                or not isinstance(security, str) or not security or side not in {"BUY", "SELL"}
                or (prior is not None and stamp < prior)):
            raise AnalysisInputError("invalid_trade_record")
        raw_qty = row.get("amount") if "amount" in row else row.get("quantity")
        qty = _decimal(raw_qty)
        price = _decimal(row.get("price"))
        commission = _decimal(row.get("commission"))
        tax = _decimal(row.get("tax"))
        if qty <= 0 or qty != int(qty) or price <= 0 or min(commission, tax) < 0:
            raise AnalysisInputError("invalid_trade_record")
        ids.add(trade_id)
        prior = stamp
        trades.append({
            "date": day,
            "security": security,
            "side": side,
            "qty": qty,
            "price": price,
            "commission": commission,
            "tax": tax,
            "name": row.get("name") or row.get("display_name"),
        })
    return trades


def _parse_corporate(native, days, day_set):
    entitlements = native.get("corporate_entitlements", {})
    if entitlements is None:
        entitlements = {}
    if not isinstance(entitlements, dict):
        raise AnalysisInputError("corporate_records_incomplete")
    last_day = days[-1]
    records_by_day = defaultdict(list)
    paid_by_day = defaultdict(list)
    ex_unpaid_by_sid = defaultdict(lambda: Decimal(0))
    share_ex_by_day = defaultdict(list)
    share_credit_by_day = defaultdict(list)
    share_adjusted = set()
    for sid, entries in entitlements.items():
        if not isinstance(sid, str) or not sid or not isinstance(entries, dict):
            raise AnalysisInputError("corporate_records_incomplete")
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise AnalysisInputError("corporate_records_incomplete")
            shares = _decimal(entry.get("shares", 0), "corporate_records_incomplete")
            cash = _decimal(entry.get("cash", 0), "corporate_records_incomplete")
            if shares < 0 or shares != int(shares) or cash < 0:
                raise AnalysisInputError("corporate_records_incomplete")
            if shares == 0 and cash == 0:
                continue
            basis = _decimal(entry.get("basis"), "corporate_records_incomplete")
            if basis <= 0 or basis != int(basis):
                raise AnalysisInputError("corporate_records_incomplete")
            stages = entry.get("stages")
            event = entry.get("event", {})
            if not isinstance(stages, dict) or not isinstance(event, dict):
                raise AnalysisInputError("corporate_records_incomplete")
            rec_stage = _stage_day(stages, "record")
            rec_day = rec_stage or (
                _optional_day(event["record_date"], "corporate_records_incomplete")
                if event.get("record_date") is not None
                else None
            )
            if rec_day is None:
                raise AnalysisInputError("corporate_records_incomplete")
            if rec_day not in day_set:
                raise AnalysisInputError("corporate_record_date_outside_result")
            records_by_day[rec_day].append((sid, basis))
            ex_day = _stage_day(stages, "ex")
            paid_day = _stage_day(stages, "cash")
            credit_day = _stage_day(stages, "stock_credit")
            declared_ex = (
                _optional_day(event["ex_date"], "corporate_records_incomplete")
                if event.get("ex_date") is not None
                else None
            )
            if cash > 0:
                model = native.get("corporate_account_model", {})
                if not isinstance(model, dict) or model.get("tax") != "gross_cash_no_tax":
                    raise AnalysisInputError("corporate_tax_model_unsupported")
                if paid_day is not None:
                    if paid_day not in day_set:
                        raise AnalysisInputError("corporate_payment_outside_result")
                    paid_by_day[paid_day].append((sid, cash))
                elif ex_day is not None:
                    if ex_day not in day_set:
                        raise AnalysisInputError("corporate_records_incomplete")
                    ex_unpaid_by_sid[sid] += cash
                elif rec_stage is not None and declared_ex is not None and declared_ex > last_day:
                    pass
                else:
                    raise AnalysisInputError("corporate_cash_unsettled")
            if shares > 0:
                effective_ex = ex_day or credit_day
                if effective_ex is None:
                    if rec_stage is not None and declared_ex is not None and declared_ex > last_day:
                        continue
                    raise AnalysisInputError("corporate_records_incomplete")
                if effective_ex not in day_set:
                    raise AnalysisInputError("corporate_records_incomplete")
                if credit_day is not None and (credit_day not in day_set or credit_day < effective_ex):
                    raise AnalysisInputError("corporate_records_incomplete")
                share_adjusted.add(sid)
                share_ex_by_day[effective_ex].append((sid, int(shares)))
                if credit_day is not None:
                    share_credit_by_day[credit_day].append((sid, int(shares)))
    return {
        "records_by_day": records_by_day,
        "paid_by_day": paid_by_day,
        "ex_unpaid_by_sid": ex_unpaid_by_sid,
        "share_ex_by_day": share_ex_by_day,
        "share_credit_by_day": share_credit_by_day,
        "share_adjusted": share_adjusted,
    }


def _collect_names(result, trades, daily, supplied):
    names = {}
    for mapping in (
        result.get("preparation", {}).get("security_names") if isinstance(result.get("preparation"), dict) else None,
        result.get("native", {}).get("security_names") if isinstance(result.get("native"), dict) else None,
        supplied,
    ):
        if isinstance(mapping, dict):
            for sid, value in mapping.items():
                if isinstance(sid, str) and isinstance(value, str) and value.strip() and value.strip() != sid:
                    names[sid] = value.strip()
    for row in trades:
        raw = row.get("name")
        sid = row["security"]
        if sid not in names and isinstance(raw, str) and raw.strip() and raw.strip() != sid:
            names[sid] = raw.strip()
    for day in daily:
        raw_positions = day.get("positions")
        if isinstance(raw_positions, dict):
            for sid, pos in raw_positions.items():
                if sid not in names and isinstance(pos, dict):
                    raw = pos.get("name") or pos.get("display_name")
                    if isinstance(raw, str) and raw.strip() and raw.strip() != sid:
                        names[sid] = raw.strip()
    return names


def _error_payload(reason, initial_dec, final_equity_dec, external_cf_dec):
    total_pnl_dec = final_equity_dec - initial_dec - external_cf_dec
    return {
        "status": "reconciliation_error",
        "reason": reason,
        "contribution_status": "unavailable",
        "contribution_reason": reason,
        "initial_equity": float(initial_dec),
        "final_equity": float(final_equity_dec),
        "external_cash_flow": float(external_cf_dec),
        "total_pnl": float(total_pnl_dec),
        "total_contribution_pp": None,
        "security_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "flat_count": 0,
        "securities_pnl": None,
        "securities_contribution_pp": None,
        "items": [],
        "account_items": [],
    }


def analyze_securities(result, *, daily=None, initial_equity=None, security_names=None):
    """Attribute portfolio PnL to individual securities and verify ledger closure."""
    native = result.get("native", {}) if isinstance(result.get("native"), dict) else {}
    summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
    if initial_equity is None:
        initial_equity = number(summary.get("initial_cash"))
    if initial_equity is None or initial_equity <= 0:
        raise AnalysisInputError("missing_initial_equity")
    if daily is None:
        from .returns import _daily_accounts

        preparation = result.get("preparation", {}) if isinstance(result.get("preparation"), dict) else {}
        daily = _daily_accounts(native.get("accounts"), preparation)

    with localcontext() as ctx:
        ctx.prec = 50
        initial_dec = _decimal(initial_equity, "missing_initial_equity")
        final_equity_dec = _decimal(daily[-1]["equity"], "invalid_equity")
        days = [row["date"] for row in daily]
        day_set = set(days)
        external_cf_dec = Decimal(0)
        try:
            has_cf, external_cf_dec, cf_by_day, cf_dated = _parse_external_cash_flows(
                native.get("external_cash_flows"), day_set
            )
            interest_dec, interest_by_day, interest_dated = _parse_cash_interest(
                native.get("cash_interest"), day_set
            )
            trades = _parse_trades(native.get("trades"), day_set)
            corp = _parse_corporate(native, days, day_set)
        except AnalysisInputError as exc:
            return _error_payload(exc.code, initial_dec, final_equity_dec, external_cf_dec)
        tol = _float_noise_tolerance(initial_dec, final_equity_dec, external_cf_dec)

        names = _collect_names(result, trades, daily, security_names)
        trades_by_day = defaultdict(list)
        for row in trades:
            trades_by_day[row["date"]].append(row)

        settled_qty = defaultdict(lambda: Decimal(0))
        pending_qty = defaultdict(lambda: Decimal(0))
        pure_cost = defaultdict(lambda: Decimal(0))
        realized_spread = defaultdict(lambda: Decimal(0))
        buy_amount = defaultdict(lambda: Decimal(0))
        sell_amount = defaultdict(lambda: Decimal(0))
        fees = defaultdict(lambda: Decimal(0))
        paid_dividend = defaultdict(lambda: Decimal(0))
        trade_count = defaultdict(int)
        involved = set(corp["share_adjusted"]) | set(corp["ex_unpaid_by_sid"])
        expected_cash = initial_dec
        check_daily_cash = cf_dated and interest_dated

        try:
            for day in daily:
                d = day["date"]
                if cf_dated:
                    expected_cash += cf_by_day[d]
                if interest_dated:
                    expected_cash += interest_by_day[d]
                for sid, added in corp["share_ex_by_day"][d]:
                    pending_qty[sid] += added
                    involved.add(sid)
                for sid, credited in corp["share_credit_by_day"][d]:
                    settled_qty[sid] += credited
                    pending_qty[sid] -= credited
                    involved.add(sid)
                for sid, cash_div in corp["paid_by_day"][d]:
                    expected_cash += cash_div
                    paid_dividend[sid] += cash_div
                    involved.add(sid)
                for trade in trades_by_day[d]:
                    sid = trade["security"]
                    qty, price = trade["qty"], trade["price"]
                    fee = trade["commission"] + trade["tax"]
                    involved.add(sid)
                    trade_count[sid] += 1
                    fees[sid] += fee
                    if trade["side"] == "BUY":
                        settled_qty[sid] += qty
                        pure_cost[sid] += qty * price
                        buy_amount[sid] += qty * price
                        expected_cash -= qty * price + fee
                    else:
                        if settled_qty[sid] <= 0 or qty > settled_qty[sid]:
                            raise AnalysisInputError("unmatched_position_history")
                        if sid not in corp["share_adjusted"]:
                            basis_cost = pure_cost[sid] * qty / settled_qty[sid]
                            realized_spread[sid] += qty * price - basis_cost
                            pure_cost[sid] -= basis_cost
                        settled_qty[sid] -= qty
                        if settled_qty[sid] == 0:
                            pure_cost[sid] = Decimal(0)
                        sell_amount[sid] += qty * price
                        expected_cash += qty * price - fee

                for sid, basis in corp["records_by_day"][d]:
                    if settled_qty[sid] != basis:
                        raise AnalysisInputError("corporate_record_basis_mismatch")

                actual_cash = day.get("cash")
                if actual_cash is None:
                    raise AnalysisInputError("missing_account_cash")
                raw_locked = day.get("locked_cash")
                locked_dec = Decimal(0) if raw_locked is None else _decimal(raw_locked, "missing_account_cash")
                actual_cash_dec = _decimal(actual_cash, "missing_account_cash") + locked_dec
                if check_daily_cash and abs(expected_cash - actual_cash_dec) > tol:
                    raise AnalysisInputError("cash_ledger_mismatch")

                raw_positions = day.get("positions")
                if not isinstance(raw_positions, dict):
                    raise AnalysisInputError("missing_position_snapshots")
                observed_non_zero = set()
                for sid, pos in raw_positions.items():
                    if not isinstance(pos, dict):
                        raise AnalysisInputError("invalid_position_snapshot")
                    raw_q = pos.get("total_amount") if "total_amount" in pos else pos.get("quantity")
                    raw_v = pos.get("value") if "value" in pos else pos.get("market_value")
                    q_num, v_num = number(raw_q), number(raw_v)
                    if q_num is None or v_num is None:
                        raise AnalysisInputError("invalid_position_snapshot")
                    if q_num != 0:
                        observed_non_zero.add(sid)
                        involved.add(sid)
                        if _decimal(q_num, "invalid_position_snapshot") != settled_qty[sid]:
                            raise AnalysisInputError("unmatched_position_history")
                expected_non_zero = {sid for sid, q in settled_qty.items() if q != 0}
                if observed_non_zero != expected_non_zero:
                    raise AnalysisInputError("unmatched_position_history")

            if not cf_dated:
                expected_cash += external_cf_dec
            if not interest_dated:
                expected_cash += interest_dec
            final_day = daily[-1]
            final_locked = (
                Decimal(0)
                if final_day.get("locked_cash") is None
                else _decimal(final_day["locked_cash"], "missing_account_cash")
            )
            final_cash_dec = _decimal(final_day["cash"], "missing_account_cash") + final_locked
            if abs(expected_cash - final_cash_dec) > tol:
                raise AnalysisInputError("cash_ledger_mismatch")

            final_positions = final_day["positions"]
            has_entitlement_dict = "corporate_entitlements" in native
            ex_unpaid_by_sid = defaultdict(lambda: Decimal(0), corp["ex_unpaid_by_sid"])
            for sid, pos in final_positions.items():
                if pos.get("dividend_receivable") is not None:
                    actual_rec = _decimal(pos["dividend_receivable"], "invalid_position_snapshot")
                    if has_entitlement_dict:
                        if abs(actual_rec - ex_unpaid_by_sid[sid]) > tol:
                            raise AnalysisInputError("corporate_record_basis_mismatch")
                    elif actual_rec > 0:
                        ex_unpaid_by_sid[sid] += actual_rec
                        involved.add(sid)

            contribution_ok = not has_cf
            raw_items = []
            securities_pnl_dec = Decimal(0)
            for sid in sorted(involved):
                pos_end = final_positions.get(sid, {})
                end_qty = int(settled_qty[sid])
                pend_qty = int(pending_qty[sid])
                settled_val = (
                    _decimal(
                        pos_end.get("value") if "value" in pos_end else pos_end.get("market_value", 0),
                        "invalid_position_snapshot",
                    )
                    if end_qty > 0
                    else Decimal(0)
                )
                pending_val = Decimal(0)
                if pend_qty > 0:
                    if pos_end.get("pending_share_value") is not None:
                        pending_val = _decimal(pos_end["pending_share_value"], "invalid_position_snapshot")
                    elif number(pos_end.get("price")) is not None:
                        pending_val = Decimal(pend_qty) * _decimal(pos_end["price"], "invalid_position_snapshot")
                    else:
                        raise AnalysisInputError("missing_pending_share_valuation")
                ending_val = settled_val + pending_val
                share_adj = sid in corp["share_adjusted"]
                if share_adj:
                    realized_val = None
                    unrealized_val = None
                    net_pnl_dec = (
                        sell_amount[sid] - buy_amount[sid] + ending_val + paid_dividend[sid] - fees[sid]
                    )
                else:
                    unrealized_dec = settled_val - pure_cost[sid]
                    realized_val = float(realized_spread[sid])
                    unrealized_val = float(unrealized_dec)
                    net_pnl_dec = realized_spread[sid] + unrealized_dec + paid_dividend[sid] - fees[sid]
                securities_pnl_dec += net_pnl_dec
                holding = end_qty > 0 or pend_qty > 0
                raw_items.append({
                    "_sort_pnl": net_pnl_dec,
                    "security": sid,
                    "name": names.get(sid),
                    "position_status": "holding" if holding else "closed",
                    "ending_quantity": end_qty,
                    "pending_shares": pend_qty,
                    "ending_value": float(ending_val),
                    "trade_count": trade_count[sid],
                    "buy_amount": float(buy_amount[sid]),
                    "sell_amount": float(sell_amount[sid]),
                    "net_pnl": float(net_pnl_dec),
                    "contribution_pp": float(net_pnl_dec / initial_dec * 100) if contribution_ok else None,
                    "breakdown_status": "unavailable" if share_adj else "ok",
                    "breakdown_reason": "corporate_share_adjustment" if share_adj else None,
                    "realized_spread": realized_val,
                    "unrealized_spread": unrealized_val,
                    "dividend_cash": float(paid_dividend[sid]),
                    "fees": float(fees[sid]),
                })

            total_ex_unpaid_dec = sum(ex_unpaid_by_sid.values(), Decimal(0))
            total_pnl_dec = final_equity_dec - initial_dec - external_cf_dec
            attributed_dec = securities_pnl_dec + interest_dec + total_ex_unpaid_dec
            if abs(attributed_dec - total_pnl_dec) > tol:
                raise AnalysisInputError("equity_reconciliation_mismatch")
        except AnalysisInputError as exc:
            return _error_payload(exc.code, initial_dec, final_equity_dec, external_cf_dec)

        raw_items.sort(key=lambda item: (-item["_sort_pnl"], item["security"]))
        zero_tol = Decimal("1e-9")
        wins = sum(1 for item in raw_items if item["_sort_pnl"] > zero_tol)
        losses = sum(1 for item in raw_items if item["_sort_pnl"] < -zero_tol)
        flats = len(raw_items) - wins - losses
        for item in raw_items:
            item.pop("_sort_pnl")

        account_items = []
        if abs(interest_dec) > zero_tol:
            account_items.append({
                "key": "cash_interest",
                "label": "现金利息",
                "pnl": float(interest_dec),
                "contribution_pp": float(interest_dec / initial_dec * 100) if contribution_ok else None,
            })
        if abs(total_ex_unpaid_dec) > zero_tol:
            account_items.append({
                "key": "ex_unpaid_dividend",
                "label": "期末已除息未到账分红",
                "pnl": float(total_ex_unpaid_dec),
                "contribution_pp": float(total_ex_unpaid_dec / initial_dec * 100) if contribution_ok else None,
            })

        return {
            "status": "ok",
            "reason": None,
            "contribution_status": "ok" if contribution_ok else "unavailable",
            "contribution_reason": None if contribution_ok else "external_cash_flows",
            "initial_equity": float(initial_dec),
            "final_equity": float(final_equity_dec),
            "external_cash_flow": float(external_cf_dec),
            "total_pnl": float(total_pnl_dec),
            "total_contribution_pp": float(total_pnl_dec / initial_dec * 100) if contribution_ok else None,
            "security_count": len(raw_items),
            "win_count": wins,
            "loss_count": losses,
            "flat_count": flats,
            "securities_pnl": float(securities_pnl_dec),
            "securities_contribution_pp": (
                float(securities_pnl_dec / initial_dec * 100) if contribution_ok else None
            ),
            "items": raw_items,
            "account_items": account_items,
        }
