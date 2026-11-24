"""Security-level PnL attribution, corporate actions, and ledger closure tests."""

import json

import pytest
from fastapi.testclient import TestClient

from ashare_quant.analysis import AnalysisPolicy, analyze_result, analyze_securities
from ashare_quant.api import create_app
from tests.support.daily_release import fixed_release as fixed_release
from tests.support.result_history import seed_python

POLICY = AnalysisPolicy(
    annualization=252,
    risk_free_annual=0.0,
    risk_free_source="assumed_zero",
    min_risk_observations=2,
)
SHA = "0" * 64


def test_single_security_closed_changjiang_power_acceptance_case():
    days = ["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06"]
    trades = [
        {
            "trade_id": "t1",
            "order_id": "o1",
            "security": "600900.XSHG",
            "at": "2025-03-03T09:35:00+08:00",
            "side": "BUY",
            "amount": 1000,
            "price": 28.50,
            "commission": 5.00,
            "tax": 0.00,
        },
        {
            "trade_id": "t2",
            "order_id": "o2",
            "security": "600900.XSHG",
            "at": "2025-03-06T14:50:00+08:00",
            "side": "SELL",
            "amount": 1000,
            "price": 29.37,
            "commission": 5.00,
            "tax": 2.97,
        },
    ]
    accounts = [
        {
            "at": "2025-03-03T15:30:00+08:00",
            "available_cash": 71495.00,
            "locked_cash": 0.0,
            "positions_value": 28600.00,
            "total_value": 100095.00,
            "positions": {
                "600900.XSHG": {"total_amount": 1000, "closeable_amount": 0, "price": 28.60, "value": 28600.00}
            },
        },
        {
            "at": "2025-03-04T15:30:00+08:00",
            "available_cash": 71495.00,
            "locked_cash": 0.0,
            "positions_value": 28400.00,
            "total_value": 100338.16,
            "positions": {
                "600900.XSHG": {
                    "total_amount": 1000,
                    "closeable_amount": 1000,
                    "price": 28.40,
                    "value": 28400.00,
                    "dividend_receivable": 443.16,
                }
            },
        },
        {
            "at": "2025-03-05T15:30:00+08:00",
            "available_cash": 71938.16,
            "locked_cash": 0.0,
            "positions_value": 29000.00,
            "total_value": 100938.16,
            "positions": {
                "600900.XSHG": {
                    "total_amount": 1000,
                    "closeable_amount": 1000,
                    "price": 29.00,
                    "value": 29000.00,
                    "dividend_receivable": 0.0,
                }
            },
        },
        {
            "at": "2025-03-06T15:30:00+08:00",
            "available_cash": 101300.19,
            "locked_cash": 0.0,
            "positions_value": 0.0,
            "total_value": 101300.19,
            "positions": {},
        },
    ]
    source = {
        "job_id": "m2job-" + "1" * 32,
        "summary": {"initial_cash": 100000.0, "net_return": 0.0130019},
        "preparation": {"trading_dates": days, "security_names": {"600900.XSHG": "长江电力"}},
        "native": {
            "preflight": {"frequency": "1m"},
            "trades": trades,
            "orders": [],
            "accounts": accounts,
            "corporate_account_model": {"tax": "gross_cash_no_tax"},
            "corporate_entitlements": {
                "600900.XSHG": {
                    "div-2025": {
                        "basis": 1000,
                        "cash": "443.16",
                        "shares": 0,
                        "event": {"record_date": "2025-03-03", "ex_date": "2025-03-04", "payment_date": "2025-03-05"},
                        "stages": {
                            "div-2025:record": "2025-03-03",
                            "div-2025:ex": "2025-03-04",
                            "div-2025:cash": "2025-03-05",
                        },
                    }
                }
            },
        },
    }
    out = analyze_result(source, source_sha256=SHA, policy=POLICY, calculation_version="returns-v2")
    sec = out["securities"]
    assert sec["status"] == "ok"
    assert sec["contribution_status"] == "ok"
    assert sec["security_count"] == 1
    assert sec["win_count"] == 1 and sec["loss_count"] == 0 and sec["flat_count"] == 0
    assert sec["account_items"] == []
    assert sec["total_pnl"] == pytest.approx(1300.19)
    assert sec["total_contribution_pp"] == pytest.approx(1.30019)
    item = sec["items"][0]
    assert item["security"] == "600900.XSHG"
    assert item["name"] == "长江电力"
    assert item["position_status"] == "closed"
    assert item["ending_quantity"] == 0
    assert item["realized_spread"] == pytest.approx(870.00)
    assert item["unrealized_spread"] == pytest.approx(0.00)
    assert item["dividend_cash"] == pytest.approx(443.16)
    assert item["fees"] == pytest.approx(12.97)
    assert item["net_pnl"] == pytest.approx(1300.19)
    assert item["contribution_pp"] == pytest.approx(1.30019)
    assert item["trade_count"] == 2
    assert item["buy_amount"] == pytest.approx(28500.00)
    json.dumps(out, allow_nan=False)


def test_multi_security_ranking_ex_unpaid_dividend_and_cash_interest_close_to_total():
    days = ["2025-04-01", "2025-04-02"]
    # Stock A: closed win (+500 spread - 10 fees = +490)
    # Stock B: holding loss (buy 1000 @ 10 = 10000, fee 5; ex-dividend 200 unpaid at end; end price 9.5 -> value 9500)
    #   Stock B pure unrealized spread = -500, paid div = 0, fees = 5 -> net_pnl = -505
    #   Account ex_unpaid_dividend = +200
    # Cash interest on 2025-04-02 = +25.50
    # Total PnL = 490 + (-505) + 25.50 + 200 = +210.50
    trades = [
        {
            "trade_id": "1",
            "order_id": "1",
            "security": "000001.XSHE",
            "at": "2025-04-01T09:35:00+08:00",
            "side": "BUY",
            "amount": 1000,
            "price": 10.0,
            "commission": 5.0,
            "tax": 0.0,
        },
        {
            "trade_id": "2",
            "order_id": "2",
            "security": "600036.XSHG",
            "at": "2025-04-01T10:00:00+08:00",
            "side": "BUY",
            "amount": 1000,
            "price": 10.0,
            "commission": 5.0,
            "tax": 0.0,
        },
        {
            "trade_id": "3",
            "order_id": "3",
            "security": "000001.XSHE",
            "at": "2025-04-02T10:00:00+08:00",
            "side": "SELL",
            "amount": 1000,
            "price": 10.5,
            "commission": 5.0,
            "tax": 0.0,
        },
    ]
    accounts = [
        {
            "at": "2025-04-01T15:30:00+08:00",
            "available_cash": 79990.0,
            "locked_cash": 0.0,
            "positions_value": 20000.0,
            "total_value": 99990.0,
            "positions": {
                "000001.XSHE": {"total_amount": 1000, "value": 10000.0, "price": 10.0},
                "600036.XSHG": {"total_amount": 1000, "value": 10000.0, "price": 10.0},
            },
        },
        {
            "at": "2025-04-02T15:30:00+08:00",
            "available_cash": 90510.5,
            "locked_cash": 0.0,
            "positions_value": 9500.0,
            "total_value": 100210.5,
            "positions": {
                "600036.XSHG": {
                    "total_amount": 1000,
                    "value": 9500.0,
                    "price": 9.5,
                    "dividend_receivable": 200.0,
                }
            },
        },
    ]
    source = {
        "job_id": "m2job-" + "2" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "preflight": {"frequency": "1d"},
            "trades": trades,
            "accounts": accounts,
            "cash_interest": [{"date": "2025-04-02", "amount": "25.50"}],
            "corporate_account_model": {"tax": "gross_cash_no_tax"},
            "corporate_entitlements": {
                "600036.XSHG": {
                    "div": {
                        "basis": 1000,
                        "cash": "200.00",
                        "shares": 0,
                        "event": {"record_date": "2025-04-01", "ex_date": "2025-04-02", "payment_date": "2025-04-07"},
                        "stages": {"div:record": "2025-04-01", "div:ex": "2025-04-02"},
                    }
                }
            },
        },
    }
    out = analyze_result(
        source,
        source_sha256=SHA,
        policy=POLICY,
        calculation_version="returns-v2",
        security_names={"000001.XSHE": "平安银行", "600036.XSHG": "招商银行"},
    )
    sec = out["securities"]
    assert sec["status"] == "ok"
    assert [i["security"] for i in sec["items"]] == ["000001.XSHE", "600036.XSHG"]
    assert [i["name"] for i in sec["items"]] == ["平安银行", "招商银行"]
    assert sec["win_count"] == 1 and sec["loss_count"] == 1 and sec["flat_count"] == 0
    assert sec["items"][0]["net_pnl"] == pytest.approx(490.0)
    assert sec["items"][1]["position_status"] == "holding"
    assert sec["items"][1]["ending_quantity"] == 1000
    assert sec["items"][1]["unrealized_spread"] == pytest.approx(-500.0)
    assert sec["items"][1]["net_pnl"] == pytest.approx(-505.0)
    keys = {item["key"]: item for item in sec["account_items"]}
    assert keys["cash_interest"]["pnl"] == pytest.approx(25.50)
    assert keys["ex_unpaid_dividend"]["pnl"] == pytest.approx(200.0)
    total_from_parts = sum(i["net_pnl"] for i in sec["items"]) + sum(i["pnl"] for i in sec["account_items"])
    contrib_from_parts = sum(i["contribution_pp"] for i in sec["items"]) + sum(
        i["contribution_pp"] for i in sec["account_items"]
    )
    assert total_from_parts == pytest.approx(sec["total_pnl"]) == pytest.approx(210.5)
    assert contrib_from_parts == pytest.approx(sec["total_contribution_pp"]) == pytest.approx(
        out["metrics"]["net_return"]["value"] * 100
    )


def test_empty_backtest_with_and_without_cash_interest():
    days = ["2025-05-06", "2025-05-07"]
    flat = {
        "job_id": "m2job-" + "3" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "trades": [],
            "accounts": [
                {"at": d + "T15:30:00+08:00", "available_cash": 100000.0, "positions_value": 0.0, "total_value": 100000.0, "positions": {}}
                for d in days
            ],
        },
    }
    sec_flat = analyze_securities(flat)
    assert sec_flat["status"] == "ok"
    assert sec_flat["security_count"] == 0
    assert sec_flat["items"] == []
    assert sec_flat["account_items"] == []
    assert sec_flat["total_pnl"] == 0.0

    with_interest = {
        "job_id": "m2job-" + "4" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "trades": [],
            "cash_interest": 88.50,
            "accounts": [
                {"at": "2025-05-06T15:30:00+08:00", "available_cash": 100040.0, "positions_value": 0.0, "total_value": 100040.0, "positions": {}},
                {"at": "2025-05-07T15:30:00+08:00", "available_cash": 100088.5, "positions_value": 0.0, "total_value": 100088.5, "positions": {}},
            ],
        },
    }
    sec_int = analyze_securities(with_interest)
    assert sec_int["status"] == "ok"
    assert sec_int["security_count"] == 0
    assert sec_int["items"] == []
    assert sec_int["account_items"] == [
        {"key": "cash_interest", "label": "现金利息", "pnl": 88.5, "contribution_pp": pytest.approx(0.0885)}
    ]
    assert sec_int["total_pnl"] == pytest.approx(88.5)


def test_bonus_share_adjustment_keeps_exact_pnl_and_null_spreads():
    days = ["2025-06-02", "2025-06-03", "2025-06-04"]
    # Buy 1000 @ 14.0 = 14000, fee 5 -> cash 85995
    # 10送4派2: shares +400, cash +200 credited on 2025-06-03 -> qty 1400, cash 86195
    # Sell 400 @ 11.0 = 4400, fee 9.4 -> qty 1000, cash 90585.6; remaining 1000 @ 11.0 = 11000 -> equity 101585.6
    source = {
        "job_id": "m2job-" + "5" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "trades": [
                {
                    "trade_id": "b1",
                    "order_id": "b1",
                    "security": "600519.XSHG",
                    "at": "2025-06-02T09:35:00+08:00",
                    "side": "BUY",
                    "amount": 1000,
                    "price": 14.0,
                    "commission": 5.0,
                    "tax": 0.0,
                },
                {
                    "trade_id": "s1",
                    "order_id": "s1",
                    "security": "600519.XSHG",
                    "at": "2025-06-04T10:00:00+08:00",
                    "side": "SELL",
                    "amount": 400,
                    "price": 11.0,
                    "commission": 5.0,
                    "tax": 4.4,
                },
            ],
            "accounts": [
                {
                    "at": "2025-06-02T15:30:00+08:00",
                    "available_cash": 85995.0,
                    "locked_cash": 0.0,
                    "positions_value": 14000.0,
                    "total_value": 99995.0,
                    "positions": {"600519.XSHG": {"total_amount": 1000, "value": 14000.0, "price": 14.0}},
                },
                {
                    "at": "2025-06-03T15:30:00+08:00",
                    "available_cash": 86195.0,
                    "locked_cash": 0.0,
                    "positions_value": 14700.0,
                    "total_value": 100895.0,
                    "positions": {"600519.XSHG": {"total_amount": 1400, "value": 14700.0, "price": 10.5}},
                },
                {
                    "at": "2025-06-04T15:30:00+08:00",
                    "available_cash": 90585.6,
                    "locked_cash": 0.0,
                    "positions_value": 11000.0,
                    "total_value": 101585.6,
                    "positions": {"600519.XSHG": {"total_amount": 1000, "value": 11000.0, "price": 11.0}},
                },
            ],
            "corporate_account_model": {"tax": "gross_cash_no_tax"},
            "corporate_entitlements": {
                "600519.XSHG": {
                    "plan": {
                        "basis": 1000,
                        "cash": "200.00",
                        "shares": 400,
                        "event": {"record_date": "2025-06-02", "ex_date": "2025-06-03"},
                        "stages": {
                            "plan:record": "2025-06-02",
                            "plan:ex": "2025-06-03",
                            "plan:cash": "2025-06-03",
                            "plan:stock_credit": "2025-06-03",
                            "plan:stock_tradable": "2025-06-04",
                        },
                    }
                }
            },
        },
    }
    sec = analyze_securities(source, security_names={"600519.XSHG": "贵州茅台"})
    assert sec["status"] == "ok"
    item = sec["items"][0]
    assert item["name"] == "贵州茅台"
    assert item["breakdown_status"] == "unavailable"
    assert item["breakdown_reason"] == "corporate_share_adjustment"
    assert item["realized_spread"] is None
    assert item["unrealized_spread"] is None
    assert item["dividend_cash"] == pytest.approx(200.0)
    assert item["fees"] == pytest.approx(14.4)
    assert item["net_pnl"] == pytest.approx(1585.6)
    assert item["contribution_pp"] == pytest.approx(1.5856)
    assert sec["total_pnl"] == pytest.approx(1585.6)


def test_external_cash_flows_leave_contribution_none_and_reconcile_net_pnl():
    days = ["2025-07-01", "2025-07-02"]
    source = {
        "job_id": "m2job-" + "6" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "external_cash_flows": [{"date": "2025-07-02", "amount": 50000.0}],
            "trades": [
                {
                    "trade_id": "1",
                    "order_id": "1",
                    "security": "600900.XSHG",
                    "at": "2025-07-01T10:00:00+08:00",
                    "side": "BUY",
                    "amount": 1000,
                    "price": 20.0,
                    "commission": 5.0,
                    "tax": 0.0,
                }
            ],
            "accounts": [
                {
                    "at": "2025-07-01T15:30:00+08:00",
                    "available_cash": 79995.0,
                    "positions_value": 20500.0,
                    "total_value": 100495.0,
                    "positions": {"600900.XSHG": {"total_amount": 1000, "value": 20500.0, "price": 20.5}},
                },
                {
                    "at": "2025-07-02T15:30:00+08:00",
                    "available_cash": 129995.0,
                    "positions_value": 21000.0,
                    "total_value": 150995.0,
                    "positions": {"600900.XSHG": {"total_amount": 1000, "value": 21000.0, "price": 21.0}},
                },
            ],
        },
    }
    sec = analyze_securities(source)
    assert sec["status"] == "ok"
    assert sec["contribution_status"] == "unavailable"
    assert sec["contribution_reason"] == "external_cash_flows"
    assert sec["total_pnl"] == pytest.approx(995.0)
    assert sec["total_contribution_pp"] is None
    assert sec["items"][0]["net_pnl"] == pytest.approx(995.0)
    assert sec["items"][0]["contribution_pp"] is None


@pytest.mark.parametrize(
    "mutate,expected_reason",
    [
        (lambda s: s["native"]["accounts"][-1].update(available_cash=99999.0), "cash_ledger_mismatch"),
        (lambda s: s["native"]["accounts"][-1].update(total_value=105000.0), "equity_reconciliation_mismatch"),
        (
            lambda s: s["native"]["accounts"][-1]["positions"].update(
                {"600900.XSHG": {"total_amount": 500, "value": 10000.0}}
            ),
            "unmatched_position_history",
        ),
    ],
)
def test_ledger_mismatch_fails_closed_without_residual_plug(mutate, expected_reason):
    days = ["2025-08-01"]
    source = {
        "job_id": "m2job-" + "7" * 32,
        "summary": {"initial_cash": 100000.0},
        "preparation": {"trading_dates": days},
        "native": {
            "trades": [],
            "accounts": [
                {
                    "at": "2025-08-01T15:30:00+08:00",
                    "available_cash": 100000.0,
                    "positions_value": 0.0,
                    "total_value": 100000.0,
                    "positions": {},
                }
            ],
        },
    }
    mutate(source)
    sec = analyze_securities(source)
    assert sec["status"] == "reconciliation_error"
    assert sec["reason"] == expected_reason
    assert sec["items"] == []
    assert sec["account_items"] == []


@pytest.mark.parametrize("drift", [0.05, -0.05, 0.01, -0.01, 0.005])
def test_10b_large_capital_rejects_cent_level_unexplained_drift(drift):
    days = ["2025-08-01", "2025-08-04"]
    initial = 10_000_000_000.0
    ending = initial + drift
    source = {
        "job_id": "m2job-" + "9" * 32,
        "summary": {"initial_cash": initial, "net_return": drift / initial},
        "preparation": {"trading_dates": days},
        "native": {
            "preflight": {"frequency": "1d"},
            "trades": [],
            "accounts": [
                {
                    "at": "2025-08-01T15:30:00+08:00",
                    "available_cash": initial,
                    "positions_value": 0.0,
                    "total_value": initial,
                    "positions": {},
                },
                {
                    "at": "2025-08-04T15:30:00+08:00",
                    "available_cash": ending,
                    "positions_value": 0.0,
                    "total_value": ending,
                    "positions": {},
                },
            ],
        },
    }
    out = analyze_result(source, source_sha256=SHA, policy=POLICY, calculation_version="returns-v2")
    sec = out["securities"]
    assert sec["status"] == "reconciliation_error"
    assert sec["reason"] == "cash_ledger_mismatch"
    assert sec["items"] == []
    assert sec["account_items"] == []


def test_10b_large_capital_absorbs_pure_float64_rounding_on_valid_trades():
    days = ["2025-08-01", "2025-08-04"]
    initial = 10_000_000_000.0
    # Valid Changjiang Power trade on a 10B account serialized through IEEE-754 float64
    cash_day1 = float(initial - 28505.0)
    total_day1 = float(cash_day1 + 28600.0)
    cash_day2 = float(cash_day1 + 29370.0 - 7.97)
    source = {
        "job_id": "m2job-" + "a" * 32,
        "summary": {"initial_cash": initial},
        "preparation": {"trading_dates": days},
        "native": {
            "preflight": {"frequency": "1d"},
            "trades": [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "security": "600900.XSHG",
                    "at": "2025-08-01T09:35:00+08:00",
                    "side": "BUY",
                    "amount": 1000,
                    "price": 28.50,
                    "commission": 5.00,
                    "tax": 0.00,
                },
                {
                    "trade_id": "t2",
                    "order_id": "o2",
                    "security": "600900.XSHG",
                    "at": "2025-08-04T14:50:00+08:00",
                    "side": "SELL",
                    "amount": 1000,
                    "price": 29.37,
                    "commission": 5.00,
                    "tax": 2.97,
                },
            ],
            "accounts": [
                {
                    "at": "2025-08-01T15:30:00+08:00",
                    "available_cash": cash_day1,
                    "positions_value": 28600.0,
                    "total_value": total_day1,
                    "positions": {"600900.XSHG": {"total_amount": 1000, "value": 28600.0, "price": 28.60}},
                },
                {
                    "at": "2025-08-04T15:30:00+08:00",
                    "available_cash": cash_day2,
                    "positions_value": 0.0,
                    "total_value": cash_day2,
                    "positions": {},
                },
            ],
        },
    }
    sec = analyze_securities(source)
    assert sec["status"] == "ok"
    assert sec["items"][0]["net_pnl"] == pytest.approx(857.03, abs=1e-4)


def test_http_analysis_resolves_security_name_from_pinned_release(tmp_path, fixed_release, monkeypatch):
    market, calendar = fixed_release
    sid = "600000.XSHG"
    with market.metadata() as db:
        expected_name = db.execute(
            "SELECT i.name FROM release_identifiers r JOIN release_instruments i USING(instrument_id) WHERE r.security=?",
            [sid],
        ).fetchone()[0]
    day = calendar[20]
    result = {
        "job_id": "m2job-" + "8" * 32,
        "name": "Pinned Name Check",
        "summary": {"initial_cash": 100000.0, "final_equity": 100095.0, "net_return": 0.00095, "max_drawdown": 0.0},
        "preparation": {"trading_dates": [day]},
        "native": {
            "preflight": {"frequency": "1d"},
            "benchmark": {"status": "not_selected"},
            "orders": [],
            "trades": [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "security": sid,
                    "at": day + "T09:35:00+08:00",
                    "side": "BUY",
                    "amount": 100,
                    "price": 10.0,
                    "commission": 5.0,
                    "tax": 0.0,
                }
            ],
            "accounts": [
                {
                    "at": day + "T15:30:00+08:00",
                    "available_cash": 98995.0,
                    "locked_cash": 0.0,
                    "positions_value": 1100.0,
                    "total_value": 100095.0,
                    "positions": {sid: {"total_amount": 100, "value": 1100.0, "price": 11.0}},
                }
            ],
        },
    }
    app = create_app(str(tmp_path / "control.sqlite3"), diagnostics_configuration={})
    seed_python(app, result, session_fields={"release": market.receipt()})
    monkeypatch.setattr(app.state.product, "call", lambda *a: (_ for _ in ()).throw(AssertionError("no live call")))
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/history/jobs/{result['job_id']}/result/analysis",
            params={"analysis_version": "returns-v2", "benchmark": "frozen"},
        )
    assert resp.status_code == 200, resp.text
    sec = resp.json()["securities"]
    assert sec["status"] == "ok"
    assert sec["items"][0]["security"] == sid
    assert sec["items"][0]["name"] == expected_name
