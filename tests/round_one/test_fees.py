import json
import os
import subprocess
import sys
from decimal import Decimal
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def invoke(case, output, variant="single_session"):
    paths = [
        str(ROOT / "src"),
        str(metadata.distribution("rqalpha").locate_file("")),
        str(Path(__file__).parent),
        str(Path(__file__).parent.parent / "execution_six"),
    ]
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['fixture',*a];from round_fee_fixture import main;main()"
    env = dict(
        PATH=os.defpath,
        TZ="Asia/Shanghai",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            boot,
            json.dumps([paths, [case, str(output), variant]]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "case,quantities,fees",
    [("B15", [100, 100], [Decimal("5"), Decimal("5")]), ("B19", [300], [Decimal("1.809")])],
)
def test_native_fees_cash_and_completed_reentry(case, quantities, fees, tmp_path):
    output = tmp_path / case
    run = invoke(case, output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    assert result["status"] == "SYNTHETIC_SCENARIO_EXECUTED"
    assert [t["quantity"] for t in result["trades"]] == quantities
    for t, fee in zip(result["trades"], fees, strict=True):
        assert abs(Decimal(t["commission"]) - fee) < Decimal("1e-12")
        assert Decimal(t["tax"]) == 0
    cash = Decimal("1000000") - sum(quantities) * Decimal("20.10") - sum(fees)
    assert abs(Decimal(result["final_account"]["cash"]) - cash) < Decimal("1e-9")
    assert result["final_account"]["positions"][0]["quantity"] == sum(quantities)
    sealed = (output / "complete.json").read_bytes()
    second = invoke(case, output)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["native_run"] is False
    assert (output / "complete.json").read_bytes() == sealed


def test_missing_market_and_unknown_overnight_refuse(tmp_path):
    missing = invoke("B15", tmp_path / "missing", "missing_bar")
    assert missing.returncode != 0
    assert "SYNTHETIC_OWNER_BAR_UNAVAILABLE" in missing.stderr
    assert not (tmp_path / "missing" / "complete.json").exists()
    overnight = invoke("B14", tmp_path / "overnight")
    assert overnight.returncode != 0
    assert "OVERNIGHT_CALENDAR_ACTION_COVERAGE_NOT_ADMITTED" in overnight.stderr
    assert not (tmp_path / "overnight" / "complete.json").exists()


def test_opt_in_native_cash_model_is_frozen_and_projected_without_second_account(tmp_path):
    output = tmp_path / "enabled"
    run = invoke("B15", output, "cash_model")
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["native_account_owner"].endswith("cash_account.CashAccount")
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    assert Decimal(result["final_account"]["transferable_cash"]) == Decimal("995970")
    assert result["final_account"]["cash_settlement"]["jq_equivalence"] == "NOT_VERIFIED"
    assert (
        json.loads((output / "native-config.json").read_text())["mod"]["round_one"]["cash_model"]
        == "aq.stock-cash-settlement.v1"
    )
    refused = invoke("B15", tmp_path / "unknown", "unknown_cash_model")
    assert refused.returncode != 0 and "CASH_EXECUTION_MODEL_UNKNOWN" in refused.stderr
    assert not (tmp_path / "unknown" / "complete.json").exists()


@pytest.mark.parametrize(
    "case,raw,filled,cash",
    [
        ("B29", "200", 100, "785"),
        ("B30", "200000.00", 16500, "800350"),
        ("B32", "250", 200, "997580"),
    ],
)
def test_native_portfolio_sizing_preserves_raw_intent(case, raw, filled, cash, tmp_path):
    output = tmp_path / case
    run = invoke(case, output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    assert result["orders"][0]["requested"] == filled
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    request = next(e for e in events if e["kind"] == "order_request")
    assert Decimal(request["requested"]) == Decimal(raw)
    assert sum(t["quantity"] for t in result["trades"]) == filled
    assert Decimal(result["final_account"]["cash"]) == Decimal(cash)


@pytest.mark.parametrize("case", ["B36", "B37", "B38", "B39"])
def test_native_lifecycle_and_caught_query_error_continue(case, tmp_path):
    output = tmp_path / case
    run = invoke(case, output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    callbacks = [e for e in events if e["kind"] == "callback"]
    assert [e["name"] for e in callbacks] == [
        "before_trading_start",
        "run_daily",
        "run_daily",
        "after_trading_end",
        "on_strategy_end",
    ]
    assert all(e["previous_date"] is None for e in callbacks)
    if case == "B37":
        immediate = next(e for e in events if e.get("label") == "immediate_after_return")
        assert immediate["orders"][0]["filled"] == 100
        assert immediate["account"]["positions"][0]["quantity"] == 100
    elif case in {"B38", "B39"}:
        failed = next(e["seq"] for e in events if e["kind"] == "query_exception")
        continued = next(e["seq"] for e in callbacks if "T10:01:" in e["at"])
        assert continued > failed
        assert len([e for e in events if e["kind"] == "query_return"]) == 1
        assert result["orders"] == [] and result["trades"] == []


def test_two_securities_share_one_native_cash_account(tmp_path):
    output = tmp_path / "B31"
    run = invoke("B31", output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    assert [(t["security"], t["quantity"], Decimal(t["price"])) for t in result["trades"]] == [
        ("000001.XSHE", 100, Decimal("12")),
        ("600000.XSHG", 200, Decimal("8")),
    ]
    assert Decimal(result["final_account"]["cash"]) == 200
    assert Decimal(result["final_account"]["total_value"]) == 3000
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["kind"] == "decision"]
    assert [Decimal(e["cash_before"]) for e in decisions] == [3000, 1800]
    assert [e["requested"] for e in decisions] == ["100", "300"]


def test_b24_queries_after_native_close_valuation(tmp_path):
    output = tmp_path / "B24"
    run = invoke("B24", output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    request = next(e for e in events if e["kind"] == "query_request")
    assert request["at"] == "2024-03-04T15:30:00+08:00"
    assert next(e["seq"] for e in events if e.get("label") == "end_of_day") < request["seq"]
    assert len(result["trades"]) == 1
