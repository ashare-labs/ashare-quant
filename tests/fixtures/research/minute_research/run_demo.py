"""Run both actual Python scripts; audit native economics and fresh replay equality."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ashare_quant.backtests.minute import run_backtest

CONFIG = {
    "initial_cash": 100000,
    "commission_rate": 0.0003,
    "min_commission": 5,
    "sell_tax": 0.0005,
    "volume_percent": 0.25,
}


def economic_audit(result, input_path):
    """Independent reconciliation, never used by the strategy or matching engine."""
    doc = json.loads(Path(input_path).read_text())
    source = {(r["security"], r["source_label"]): r for r in doc["minute"]}
    orders = {o["order_id"]: o for o in result["orders"]}
    cash, bought = Decimal(str(CONFIG["initial_cash"])), {}
    for trade in result["trades"]:
        row = source[trade["security"], trade["at"]]
        submitted = datetime.fromisoformat(orders[trade["order_id"]]["add_time"])
        assert datetime.fromisoformat(trade["at"]).replace(tzinfo=None) == submitted + timedelta(
            minutes=1
        )
        assert Decimal(str(trade["price"])) == Decimal(row["open"])
        assert trade["amount"] <= int(Decimal(row["volume"]) * Decimal("0.25") / 100) * 100
        sign = 1 if trade["side"] == "BUY" else -1
        turnover = trade["amount"] * Decimal(str(trade["price"]))
        commission = max(Decimal("5"), turnover * Decimal("0.0003"))
        tax = Decimal("0") if sign == 1 else turnover * Decimal("0.0005")
        assert abs(Decimal(str(trade["commission"])) - commission) < Decimal("0.0000001")
        assert abs(Decimal(str(trade["tax"])) - tax) < Decimal("0.0000001")
        cash -= sign * turnover
        cash -= Decimal(str(trade["commission"])) + Decimal(str(trade["tax"]))
        assert cash >= 0
        if sign == 1:
            bought[trade["security"]] = trade["at"][:10]
        else:
            assert bought[trade["security"]] < trade["at"][:10]
    account = result["final_account"]
    assert not account["positions"] and abs(account["locked_cash"]) < 1e-8
    assert abs(cash - Decimal(str(account["available_cash"]))) < Decimal("0.0000001")
    assert account["total_value"] == account["available_cash"]
    return {
        "cash_from_trade_reconciliation": str(cash),
        "all_fill_prices_match_source_next_open": True,
        "source_volume_caps_checked": True,
        "configured_commission_and_tax_independently_checked": True,
        "no_same_day_sells": True,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument(
        "--accept-research",
        action="store_true",
        help="accept all bounded research assumptions described in this example's README",
    )
    args = p.parse_args()
    if not args.accept_research:
        p.error(
            "--accept-research is required; see README for clocks, action scope and price envelope"
        )
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    report = {
        "input_sha256": hashlib.sha256(Path(args.input).read_bytes()).hexdigest(),
        "config": CONFIG,
        "strict_comparison": "NOT_PERFORMED",
        "strategies": {},
    }
    for name in ("shared_cash_t1", "visible_momentum"):
        script = Path(__file__).with_name(name + ".py")
        runs = [
            run_backtest(
                script,
                args.input,
                root / (name + suffix),
                config=CONFIG,
                accept_assumed=True,
                accept_state_replay=True,
                accept_research=True,
            )
            for suffix in ("-a", "-b")
        ]
        first, second = [r["result"] for r in runs]
        assert first == second, "Fresh runs must reproduce every result field, not just final PnL"
        reentry = run_backtest(
            script,
            args.input,
            root / (name + "-a"),
            config=CONFIG,
            accept_assumed=True,
            accept_state_replay=True,
            accept_research=True,
        )
        assert not reentry["native_run"] and reentry["result"] == first
        events = [
            json.loads(s) for s in (root / (name + "-a") / "events.jsonl").read_text().splitlines()
        ]
        if name == "shared_cash_t1":
            assert {t["security"] for t in first["trades"]} == {"600000.XSHG", "000001.XSHE"}
            assert sum(e["kind"] == "order_rejection" for e in events) >= 3
            assert len(first["accounts"][0]["positions"]) == 2
        report["strategies"][name] = {
            "audit": economic_audit(first, args.input),
            "orders": len(first["orders"]),
            "trades": len(first["trades"]),
            "daily_accounts": first["accounts"],
            "final_account": first["final_account"],
            "total_pnl": first["total_pnl"],
            "total_return": first["total_return"],
            "result_replay_identical": True,
            "completed_reentry_executed": False,
        }
    assert (
        report["strategies"]["shared_cash_t1"]["total_pnl"]
        != report["strategies"]["visible_momentum"]["total_pnl"]
    )
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
