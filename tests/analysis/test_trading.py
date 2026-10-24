"""Fill/cost/corporate attribution goldens, separate from return-series metrics."""

import pytest

from ashare_quant.analysis import AnalysisPolicy, analyze_result

POLICY = AnalysisPolicy(annualization=252, risk_free_annual=0.0,
                        risk_free_source="assumed_zero", min_risk_observations=2)


def fixture():
    def fill(key, day, side, qty, price, commission=1, tax=0):
        return {"trade_id": key, "order_id": key, "security": "TEST_STOCK",
                "at": f"2025-01-0{day}T10:00:00+08:00", "side": side,
                "amount": qty, "price": price, "commission": commission, "tax": tax}

    trades = [fill("a", 2, "BUY", 2, 10), fill("b", 3, "SELL", 1, 12, tax=.1),
              fill("c", 4, "SELL", 1, 9, tax=.1), fill("d", 5, "BUY", 1, 10),
              fill("e", 6, "SELL", 1, 15), fill("f", 7, "BUY", 1, 10)]
    qty = [2, 1, 0, 1, 0, 1]
    equity = [99, 99.9, 97.8, 96.8, 100.8, 99.8]
    accounts = [{"at": f"2025-01-0{d}T15:30:00+08:00", "total_value": e,
                 "available_cash": e - q*10, "locked_cash": 0, "positions_value": q*10,
                 "positions": {"TEST_STOCK": {"total_amount": q, "value": q*10}} if q else {}}
                for d, q, e in zip(range(2, 8), qty, equity)]
    return {"job_id": "synthetic-trades", "summary": {"initial_cash": 100},
            "native": {"accounts": accounts, "trades": trades}}


def analyze(source):
    return analyze_result(source, source_sha256="0"*64, policy=POLICY)


def test_partial_closes_buy_costs_taxes_and_open_tail_are_not_extra_wins():
    out = analyze(fixture())
    closed = out["trading"]["closed_trades"]
    assert out["trading"]["status"] == "ok"
    assert [x["net_pnl"] for x in closed] == pytest.approx([-2.2, 3])
    assert out["trading"]["open_cycles"] == 1
    assert out["metrics"]["win_rate"]["value"] == .5
    assert out["metrics"]["winning_trades"]["value"] == 1
    assert out["metrics"]["losing_trades"]["value"] == 1
    assert out["metrics"]["profit_loss_ratio"]["value"] == pytest.approx(3/2.2)
    assert out["trading"]["fill_pnl"][0]["reason"] == "not_a_closing_fill"
    assert out["trading"]["fill_pnl"][1]["value"] == pytest.approx(.4)
    assert out["trading"]["fill_pnl"][2]["value"] == pytest.approx(-2.6)
    assert out["costs"]["total"] == pytest.approx(6.2)
    assert out["trading"]["positions"][-1]["unrealized_pnl"] == -1


def test_paid_cash_dividend_belongs_to_record_date_cycle_even_after_close():
    source = fixture()
    source["native"]["corporate_account_model"] = {"tax": "gross_cash_no_tax"}
    source["native"]["corporate_entitlements"] = {"TEST_STOCK": {"event": {
        "basis": 2, "cash": "4.00", "shares": 0,
        "event": {"record_date": "2025-01-02"}, "stages": {"event:cash": "2025-01-06"}}}}
    for account in source["native"]["accounts"][-2:]:
        account["available_cash"] += 4
        account["total_value"] += 4
    out = analyze(source)
    assert out["trading"]["closed_trades"][0]["net_pnl"] == pytest.approx(1.8)
    assert out["costs"]["dividend_cash"] == 4
    assert out["metrics"]["winning_trades"]["value"] == 2
    assert out["metrics"]["profit_loss_ratio"]["reason"] == "no_losing_trades"
    assert out["trading"]["fill_pnl"][2]["value"] == pytest.approx(-2.6)


@pytest.mark.parametrize("change,reason", [
    (lambda native: native["trades"][0].pop("commission"), "invalid_trade_record"),
    (lambda native: native["trades"][0].update(side="SELL"), "unmatched_position_history"),
    (lambda native: native["accounts"][0]["positions"]["TEST_STOCK"].update(total_amount=3), "unmatched_position_history"),
    (lambda native: native["accounts"][0].update(available_cash=80), "cash_ledger_mismatch"),
    (lambda native: native.update(corporate_entitlements={"TEST_STOCK": {"x": {"shares": 1}}}), "corporate_share_adjustment"),
    (lambda native: native.update(corporate_account_model={"tax": "gross_cash_no_tax"},
        corporate_entitlements={"TEST_STOCK": {"x": {"cash": 4, "shares": 0, "stages": {}}}}), "corporate_cash_unsettled"),
])
def test_unsupported_attribution_stays_unknown_but_equity_statistics_survive(change, reason):
    source = fixture()
    change(source["native"])
    out = analyze(source)
    assert out["trading"]["reason"] == reason
    assert out["metrics"]["win_rate"]["value"] is None
    assert out["metrics"]["winning_trades"]["value"] is None
    assert out["metrics"]["net_return"]["value"] == pytest.approx(-.002)
