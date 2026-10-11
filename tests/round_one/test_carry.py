import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dividend_fixture import FACT

from ashare_quant.round_one.carry_contract import (
    admit_dividend_execution,
    cash_capabilities,
    dividend_records,
    require_carry_admission,
)


def test_native_dividend_receivable_and_payment_are_not_double_booked(tmp_path):
    paths = [
        str(Path(__file__).resolve().parents[2] / "src"),
        str(metadata.distribution("rqalpha").locate_file("")),
        str(Path(__file__).parent),
    ]
    output = tmp_path / "native-dividend.json"
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['synthetic-dividend',*a];from dividend_fixture import main;main()"
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            boot,
            json.dumps([paths, ["--out", str(output), "--mode", "actions"]]),
        ],
        env=dict(
            PATH=os.defpath,
            TZ="Asia/Shanghai",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-7000:]
    r = json.loads(output.read_text())
    assert r["evidence_kind"] == "synthetic" and len(r["trades"]) == 1
    ex = next(
        x
        for x in r["trace"]
        if x["event"] == "POST_BEFORE_TRADING" and x["dt"].startswith("2024-01-03")
    )
    pay = next(
        x
        for x in r["trace"]
        if x["event"] == "POST_BEFORE_TRADING" and x["dt"].startswith("2024-01-04")
    )
    assert Decimal(str(ex["receivable"])) == Decimal("70") and Decimal(str(ex["cash"])) == Decimal(
        "98995"
    )
    assert Decimal(str(pay["receivable"])) == 0 and Decimal(str(pay["cash"])) == Decimal("99065")
    assert Decimal(str(r["state"]["cash"])) == Decimal("99065")
    assert Decimal(str(r["state"]["average"])) == Decimal("9.3")
    assert r["state"]["quantity"] == 100


def test_known_cash_fact_does_not_grant_coverage_or_strategy_visibility():
    at = datetime(2024, 1, 2, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(RuntimeError, match="DIVIDEND_ENGINE_RESEARCH_ROLE_REQUIRED"):
        dividend_records(FACT, security=FACT["security"], as_of=at, role="strategy")
    with pytest.raises(RuntimeError, match="CARRY_FACT_CAPABILITIES_UNKNOWN"):
        require_carry_admission(dict(calendar_adjacency=True, action_coverage=None, lifecycle=True))
    assert (
        cash_capabilities()["transferable_cash_status"]
        == "IMPLEMENTED_CONDITIONAL_MODEL_NOT_JQ_CERTIFIED"
    )


def test_date_only_announcement_is_not_visible_earlier_that_day():
    at = datetime(2023, 12, 29, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(RuntimeError, match="DIVIDEND_PUBLICATION_TIME_NOT_YET_RESOLVED"):
        dividend_records(
            FACT, security=FACT["security"], as_of=at, role="engine_known_event_research"
        )


def test_announcement_guard_uses_shanghai_date_and_requested_security():
    at = datetime(2023, 12, 30, 1, tzinfo=ZoneInfo("Pacific/Kiritimati"))
    with pytest.raises(RuntimeError, match="DIVIDEND_PUBLICATION_TIME_NOT_YET_RESOLVED"):
        dividend_records(
            FACT, security=FACT["security"], as_of=at, role="engine_known_event_research"
        )
    at = datetime(2024, 1, 2, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(RuntimeError, match="DIVIDEND_FACT_SCHEMA_UNSUPPORTED"):
        dividend_records(FACT, security="000001.XSHE", as_of=at, role="engine_known_event_research")


def test_dividend_model_requires_independent_qualification_timing_and_tax_choices():
    from datetime import date

    import ashare_quant.round_one.carry_contract as contract

    model = json.loads(Path(contract.__file__).with_name("dividend-model.json").read_text())
    args = dict(
        security=FACT["security"],
        as_of=datetime(2024, 1, 2, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        next_session=lambda _: date(2024, 1, 3),
        model=model,
    )
    assert len(admit_dividend_execution(FACT, **args)) == 1
    with pytest.raises(RuntimeError, match="DIVIDEND_EXPLICIT_ACCOUNT_MODEL_REQUIRED"):
        admit_dividend_execution(
            FACT, **{**args, "model": {**model, "tax": "infer_from_reference_cash"}}
        )
    with pytest.raises(RuntimeError, match="DIVIDEND_RECORD_EX_ADJACENCY_REQUIRED"):
        admit_dividend_execution(FACT, **{**args, "next_session": lambda _: date(2024, 1, 4)})
