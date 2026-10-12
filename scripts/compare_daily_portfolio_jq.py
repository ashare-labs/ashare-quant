"""Read-only comparison of a native JQ JSONL reference and a frozen AQ run.

Raw account evidence stays outside the repository.  Same-input signal checks
are reported separately from the independent-source end-to-end comparison.
"""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from ashare_quant.daily_portfolio.reference_audit import primary_audit
from ashare_quant.daily_portfolio.signals import decide, scheduled

TOL = Decimal("0.000001")


def dec(value):
    return Decimal(str(value))


def load(path):
    return json.loads(Path(path).read_text(), parse_float=Decimal)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _compare_observed_legacy(reference, aq_result, owner_input, manifest):
    events = [
        json.loads(line, parse_float=Decimal) for line in Path(reference).read_text().splitlines()
    ]
    aq, owner, spec = load(aq_result), load(owner_input), load(manifest)
    identity = events[0]
    start, end = identity["window"]
    strategy = identity["strategy"]
    days = [d for d in owner["calendar"] if start <= d <= end]
    closes = [r for r in events if r.get("label") == "day_close"]
    decisions = [r for r in events if r["kind"] == "decision"]
    checks = {
        "contiguous_sequence": [e["seq"] for e in events] == list(range(1, len(events) + 1)),
        "identity_first": identity["kind"] == "identity",
        "strategy_end_last": events[-1].get("label") == "strategy_end",
        "daily_closes_complete": [r["time"][:10] for r in closes] == days,
        "daily_decisions_complete": [r["time"][:10] for r in decisions] == days,
        "securities_frozen": identity["securities"] == spec["securities"],
        "daily_frequency": identity["frequency"] in ("day", "daily"),
        "initial_cash_frozen": dec(identity["initial_cash"])
        == dec(spec["execution"]["initial_cash"]),
        "aq_strategy_equal": strategy == aq["strategy"],
        "aq_daily_accounts_complete": [r["date"] for r in aq["accounts"]] == days,
        "aq_daily_decisions_complete": [r["date"] for r in aq["decisions"]] == days,
        "window_frozen": any(start == w["start"] and end == w["end"] for w in spec["windows"]),
        "strategy_frozen": strategy in {s["id"] for s in spec["strategies"]},
        "final_cash_and_equity_match_last_close": bool(closes)
        and all(
            abs(dec(events[-1]["account"][f]) - dec(closes[-1]["account"][f])) <= TOL
            for f in ("available_cash", "locked_cash", "positions_value", "total_value")
        ),
    }
    if not all(checks.values()):
        raise ValueError({"incomplete_or_mismatched_reference": checks})
    market = {(r["date"], r["security"]): r for r in owner["daily"]}
    histories, dates_by_sid, history_diffs, history_date_diffs = {}, {}, {}, []
    history_securities_by_day = {}
    formula_diffs, schedule_diffs, formula_count, history_count = [], [], 0, 0
    for e in events:
        day = e["time"][:10]
        if e["kind"] == "history":
            assert len(e["raw_close"]) == len(e["dates"]) == 120
            seen = history_securities_by_day.setdefault(day, [])
            assert e["security"] not in seen, "duplicate native history"
            seen.append(e["security"])
            histories[e["security"]] = e["raw_close"]
            dates_by_sid[e["security"]] = e["dates"]
            prior_dates = [d for d in owner["calendar"] if d < day][-120:]
            if e["dates"] != prior_dates:
                history_date_diffs.append({"day": day, "security": e["security"]})
            for date, value in zip(e["dates"], e["raw_close"]):
                history_count += 1
                expected = market.get((date, e["security"]), {}).get("close")
                if expected is None or dec(value) != dec(expected):
                    history_diffs[(date, e["security"])] = {
                        "date": date,
                        "security": e["security"],
                        "jq": value,
                        "ad": expected,
                    }
        if e["kind"] == "decision":
            planned = scheduled(strategy, day, e["previous_date"], day == start)
            if planned != e["rebalance"]:
                schedule_diffs.append(day)
            if e["rebalance"]:
                assert sorted(history_securities_by_day.get(day, [])) == sorted(
                    spec["securities"]
                ), "missing same-day signal input"
                assert len(histories) == len(spec["securities"])
                assert all(len(v) == 120 for v in histories.values())
                assert all(max(v) <= e["previous_date"] for v in dates_by_sid.values())
                computed = decide(strategy, histories, e["prior_equity"])
                formula_count += 1
                for field in ("selected", "scores", "weights", "targets"):
                    if computed[field] != e[field]:
                        formula_diffs.append(
                            {
                                "date": day,
                                "field": field,
                                "aq_from_jq_input": computed[field],
                                "jq": e[field],
                            }
                        )
    aq_decisions = {r["date"]: r for r in aq["decisions"]}
    decision_diffs = []
    for e in decisions:
        day = e["time"][:10]
        local = aq_decisions[day]
        if local["rebalance"] != e["rebalance"]:
            decision_diffs.append({"date": day, "field": "rebalance"})
        elif e["rebalance"]:
            for field in ("selected", "scores", "weights", "targets"):
                if local["signal"][field] != e[field]:
                    decision_diffs.append(
                        {"date": day, "field": field, "aq": local["signal"][field], "jq": e[field]}
                    )

    orders, trades = {}, {}
    for e in events:
        if e["kind"] == "snapshot":
            for order in e["orders"]:
                orders[str(order["order_id"])] = order
            for trade in e["trades"]:
                key = str(trade["trade_id"])
                if key in trades:
                    assert trades[key] == trade, "native trade changed across snapshots"
                trades[key] = trade
        elif e["kind"] == "order_return" and e["order"]:
            order = e["order"]
            orders.setdefault(str(order["order_id"]), {}).update(order)
    jq_trade_rows, fee_diffs, cash_flow = [], [], {}
    for trade in trades.values():
        order = orders[str(trade["order_id"])]
        date = trade["time"][:10]
        row = {
            "date": date,
            "time": trade["time"],
            "security": trade["security"],
            "side": "BUY" if order["is_buy"] else "SELL",
            "quantity": trade["amount"],
            "price": dec(trade["price"]),
            "order_id": str(trade["order_id"]),
        }
        jq_trade_rows.append(row)
        cash_flow[date] = cash_flow.get(date, Decimal(0)) + dec(trade["amount"]) * dec(
            trade["price"]
        ) * (-1 if order["is_buy"] else 1)
    fees_by_day = {}
    for oid, order in orders.items():
        relevant = [r for r in jq_trade_rows if r["order_id"] == oid]
        if not relevant:
            continue
        gross = sum((dec(r["quantity"]) * r["price"] for r in relevant), Decimal(0))
        expected = max(Decimal(5), gross * Decimal("0.0003")) + (
            Decimal(0) if order["is_buy"] else gross * Decimal("0.001")
        )
        fee = dec(order["commission"])
        if abs(fee - expected) > TOL:
            fee_diffs.append({"order_id": oid, "expected": expected, "jq_total_fee": fee})
        day = relevant[-1]["date"]
        fees_by_day[day] = fees_by_day.get(day, Decimal(0)) + fee

    def trade_key(row):
        return (row["date"], row["security"], row["side"], int(row["quantity"]), dec(row["price"]))

    aq_trade_counts = Counter(trade_key(t) for t in aq["trades"])
    jq_trade_counts = Counter(trade_key(t) for t in jq_trade_rows)
    unmatched_aq = [
        {"trade": k, "count": n} for k, n in (aq_trade_counts - jq_trade_counts).items()
    ]
    unmatched_jq = [
        {"trade": k, "count": n} for k, n in (jq_trade_counts - aq_trade_counts).items()
    ]
    account_diffs, position_diffs, mark_diffs, native_audit, cash_residuals = [], [], [], [], []
    cost_basis_diffs, sellable_diffs, value_diffs, native_return_timing = [], [], [], []
    max_cash, max_equity, min_cash = Decimal(0), Decimal(0), dec(identity["initial_cash"])
    prior_cash = dec(identity["initial_cash"])
    for local, close in zip(aq["accounts"], closes):
        day, account = close["time"][:10], close["account"]
        assert local["date"] == day
        cash, equity = dec(account["available_cash"]), dec(account["total_value"])
        min_cash = min(min_cash, cash)
        dc, de = cash - dec(local["cash"]), equity - dec(local["equity"])
        max_cash, max_equity = max(max_cash, abs(dc)), max(max_equity, abs(de))
        if abs(dc) > TOL or abs(de) > TOL:
            account_diffs.append({"date": day, "cash_jq_minus_aq": dc, "equity_jq_minus_aq": de})
        positions = {p["security"]: p for p in close["positions"] if p["total_amount"]}
        local_qty = {s: p["quantity"] for s, p in local["positions"].items() if p["quantity"]}
        native_qty = {s: p["total_amount"] for s, p in positions.items()}
        if local_qty != native_qty:
            position_diffs.append({"date": day, "aq": local_qty, "jq": native_qty})
        for s, p in positions.items():
            expected = market[(day, s)]["close"]
            if dec(expected) != dec(p["price"]):
                mark_diffs.append({"date": day, "security": s, "jq": p["price"], "ad": expected})
            local_position = local["positions"].get(s)
            if local_position is not None:
                if abs(dec(p["avg_cost"]) - dec(local_position["average_price"])) > TOL:
                    cost_basis_diffs.append(
                        {
                            "date": day,
                            "security": s,
                            "jq": p["avg_cost"],
                            "aq": local_position["average_price"],
                        }
                    )
                if p["closeable_amount"] != local_position["sellable"]:
                    sellable_diffs.append(
                        {
                            "date": day,
                            "security": s,
                            "jq": p["closeable_amount"],
                            "aq": local_position["sellable"],
                        }
                    )
                if abs(dec(p["value"]) - dec(local_position["value"])) > TOL:
                    value_diffs.append(
                        {
                            "date": day,
                            "security": s,
                            "jq": p["value"],
                            "aq": local_position["value"],
                        }
                    )
        equity_return = equity / dec(identity["initial_cash"]) - 1
        if abs(dec(account["returns"]) - equity_return) > Decimal("0.000000000001"):
            native_return_timing.append(
                {
                    "date": day,
                    "callback_returns": account["returns"],
                    "same_snapshot_equity_return": equity_return,
                }
            )
        mv = sum((dec(p["value"]) for p in positions.values()), Decimal(0))
        if abs(cash + dec(account["locked_cash"]) + mv - equity) > TOL:
            native_audit.append(
                {
                    "date": day,
                    "cash_plus_positions_minus_equity": cash
                    + dec(account["locked_cash"])
                    + mv
                    - equity,
                }
            )
        residual = (
            cash - prior_cash - cash_flow.get(day, Decimal(0)) + fees_by_day.get(day, Decimal(0))
        )
        if abs(residual) > TOL:
            cash_residuals.append({"date": day, "nontrade_native_cash_change": residual})
        prior_cash = cash
    all_cash = [dec(e["account"]["available_cash"]) for e in events if e["kind"] == "snapshot"]
    all_cash += [dec(e[k]) for e in events for k in ("cash_before", "cash_after") if k in e]
    submit_ordering = []
    for day in days:
        submitted = [
            (e["security"], e["quantity"])
            for e in events
            if e["kind"] == "submit" and e["time"][:10] == day
        ]
        expected = sorted(submitted, key=lambda row: (row[1] > 0, row[0]))
        if submitted != expected:
            submit_ordering.append(day)
    open_diffs, state_diffs = [], []
    for e in events:
        if e["kind"] != "submit":
            continue
        day, sid = e["time"][:10], e["security"]
        row = market[(day, sid)]
        if dec(e["market"]["day_open"]) != dec(row["open"]):
            open_diffs.append(
                {"date": day, "security": sid, "jq": e["market"]["day_open"], "ad": row["open"]}
            )
        modeled = row if "upper_limit" in row else row.get("research_state", {})
        for jf, af in (
            ("paused", "suspended"),
            ("high_limit", "upper_limit"),
            ("low_limit", "lower_limit"),
        ):
            jv, av = e["market"].get(jf), modeled.get(af)
            if (
                isinstance(jv, dict)
                or av is None
                or jv != (bool(av) if jf == "paused" else dec(av))
            ):
                state_diffs.append(
                    {
                        "date": day,
                        "security": sid,
                        "field": jf,
                        "jq_observed_in_reference": jv,
                        "ad_conditional_model": av,
                    }
                )
    return {
        "schema": "aq-jq-daily-portfolio-comparison.v1",
        "strategy": strategy,
        "window": [start, end],
        "identities": {
            "jq_jsonl_sha256": digest(reference),
            "aq_result_sha256": digest(aq_result),
            "owner_input_sha256": digest(owner_input),
            "manifest_sha256": digest(manifest),
        },
        "coverage": {
            **checks,
            "events": len(events),
            "days": len(days),
            "jq_unique_trades": len(trades),
            "aq_trades": len(aq["trades"]),
        },
        "same_input_calculation": {
            "checked_rebalances": formula_count,
            "signal_differences": formula_diffs,
            "schedule_differences": schedule_diffs,
        },
        "independent_source": {
            "history_value_observations": history_count,
            "unique_history_value_differences": list(history_diffs.values()),
            "history_calendar_differences": history_date_diffs,
            "submission_open_differences": open_diffs,
            "held_position_close_differences": mark_diffs,
            "conditional_state_model_differences": state_diffs,
        },
        "end_to_end": {
            "decision_differences": decision_diffs,
            "unmatched_aq_trades": unmatched_aq,
            "unmatched_jq_trades": unmatched_jq,
            "daily_account_differences": account_diffs,
            "daily_quantity_differences": position_diffs,
            "daily_cost_basis_differences": cost_basis_diffs,
            "daily_sellable_differences": sellable_diffs,
            "daily_position_value_differences": value_diffs,
            "max_absolute_cash_difference": max_cash,
            "max_absolute_equity_difference": max_equity,
            "aq_final": aq["final_account"],
            "jq_final": events[-1]["account"],
        },
        "native_jq_readonly_audit": {
            "fee_model_differences": fee_diffs,
            "account_equation_differences": native_audit,
            "nontrade_cash_changes": cash_residuals,
            "minimum_closing_cash": min_cash,
            "minimum_observed_cash": min(all_cash),
            "sell_before_buy_ordering_differences": submit_ordering,
            "callback_returns_vs_same_snapshot_equity": native_return_timing,
        },
        "limitations": [
            "Historical native reference is not historical availability/PIT evidence.",
            "State comparison is JQ reference versus an explicit AD model, not certification of the model.",
            "The original limited comparison uses submitted opens and held closes; primary_comparison separately requires complete raw daily and order observations.",
            "Reference commission is a platform total; separate statutory fee components are not inferred.",
            "Native cash residuals require corporate-action/tax attribution; they are not silently copied into AQ.",
        ],
    }


def compare(reference, aq_result, owner_input, manifest):
    with localcontext(prec=50):
        return _compare(reference, aq_result, owner_input, manifest)


def _compare(reference, aq_result, owner_input, manifest):
    events = [
        json.loads(line, parse_float=Decimal) for line in Path(reference).read_text().splitlines()
    ]
    aq, owner, spec = load(aq_result), load(owner_input), load(manifest)
    try:
        primary = primary_audit(events, aq, owner, spec)
    except (KeyError, TypeError, ValueError, IndexError, InvalidOperation) as exc:
        primary = {
            "differences": [],
            "contract_differences": [],
            "evidence_complete": False,
            "eligible_for_alignment": False,
            "evidence_gaps": [{"code": "MALFORMED_PRIMARY_EVIDENCE", "detail": str(exc)}],
        }
    try:
        report = _compare_observed_legacy(reference, aq_result, owner_input, manifest)
    except (KeyError, TypeError, ValueError, IndexError, AssertionError, InvalidOperation) as exc:
        report = {
            "schema": "aq-jq-daily-portfolio-comparison.v1",
            "identities": {
                "jq_jsonl_sha256": digest(reference),
                "aq_result_sha256": digest(aq_result),
                "owner_input_sha256": digest(owner_input),
                "manifest_sha256": digest(manifest),
            },
        }
        primary["evidence_gaps"].append(
            {"code": "LEGACY_REFERENCE_INCOMPLETE_OR_INVALID", "detail": str(exc)}
        )
    # Preserve the original bounded comparison without treating missing new fields
    # as equal. Existing captures do not acquire daily_raw observations retroactively.
    old_difference_keys = {
        "same_input_calculation": ("signal_differences", "schedule_differences"),
        "independent_source": (
            "unique_history_value_differences",
            "history_calendar_differences",
            "submission_open_differences",
            "held_position_close_differences",
            "conditional_state_model_differences",
        ),
        "end_to_end": (
            "decision_differences",
            "unmatched_aq_trades",
            "unmatched_jq_trades",
            "daily_account_differences",
            "daily_quantity_differences",
            "daily_cost_basis_differences",
            "daily_sellable_differences",
            "daily_position_value_differences",
        ),
        "native_jq_readonly_audit": (
            "fee_model_differences",
            "account_equation_differences",
            "sell_before_buy_ordering_differences",
        ),
    }
    legacy_count = sum(
        len(report.get(section, {}).get(key, []))
        for section, keys in old_difference_keys.items()
        for key in keys
    )
    count = legacy_count + len(primary["differences"]) + len(primary["contract_differences"])
    missing = len(primary["evidence_gaps"])
    primary["evidence_complete"] = not missing
    primary["eligible_for_alignment"] = not (count or missing)
    report["primary_comparison"] = primary
    report["acceptance"] = {
        "status": "DIFFERENCES_OBSERVED"
        if count
        else ("EVIDENCE_INSUFFICIENT" if missing else "OBSERVED_FIELDS_ALIGNED"),
        "evidence_complete": not missing,
        "eligible_for_alignment": not (count or missing),
        "difference_count": count,
        "evidence_gap_count": missing,
        "legacy_bounded_difference_count": legacy_count,
        "scope": "Only the supplied observed fields; no historical PIT or unseen platform behavior claim",
    }
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reference", required=True)
    p.add_argument("--aq-result", required=True)
    p.add_argument("--owner-input", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = compare(a.reference, a.aq_result, a.owner_input, a.manifest)
    Path(a.output).write_text(json.dumps(result, default=str, indent=2, ensure_ascii=False) + "\n")
    status = result["acceptance"]["status"]
    return (
        0 if status == "OBSERVED_FIELDS_ALIGNED" else (2 if status == "DIFFERENCES_OBSERVED" else 3)
    )


if __name__ == "__main__":
    raise SystemExit(main())
