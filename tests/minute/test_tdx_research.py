"""Real-source integration tests consume the separately delivered hash-bound input.

The market data is intentionally not redistributed in the code wheel/sdist.
Set AQ_TDX_RESEARCH_INPUT to the export from the documented fixed AD captures.
Mutation cases are fault injections, not additional real-market experiments.
"""

import hashlib
import json
import os
import runpy
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.backtests.minute import MinuteInputs, run_backtest
from ashare_quant.backtests.minute.bounded import check_price, envelope
from ashare_quant.backtests.minute.runner import MinuteRunError

INPUT_SHA = "8e9320af8ac8b5c51dc5ca67112199c8969fe3a34a71d1a20497d24a9d2c52b2"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples/minute_research"
ACCEPT = dict(accept_assumed=True, accept_state_replay=True, accept_research=True)


@pytest.fixture
def real_input():
    name = os.environ.get("AQ_TDX_RESEARCH_INPUT")
    if not name:
        pytest.skip("Frozen real TDX research export is a separate local evidence delivery")
    path = Path(name).resolve()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == INPUT_SHA
    return path


@pytest.fixture
def real_doc(real_input):
    return json.loads(real_input.read_text())


def test_inward_envelope_is_a_restriction_not_exchange_bands():
    assert envelope("9.701") == (Decimal("9.32"), Decimal("10.08"))
    for low, high in [("9.60", "10"), ("10", "10.40"), ("9.59", "10.41")]:
        with pytest.raises(ValueError, match="PRICE_ENVELOPE_TOUCHED"):
            check_price({"previous_close": "10"}, low, high)


def test_research_optin_does_not_waive_original_strict_fields(minute_doc, tmp_path):
    minute_doc["research"] = {"purpose": "retrospective_research"}
    path = tmp_path / "strict.json"
    path.write_text(json.dumps(minute_doc))
    with pytest.raises(ValueError, match="MINUTE_RESEARCH_SCHEMA_REQUIRED"):
        MinuteInputs(path, **ACCEPT)


@pytest.mark.parametrize("flag", list(ACCEPT))
def test_all_three_distinct_assumptions_need_optin(real_input, flag):
    with pytest.raises(ValueError, match="REQUIRES_OPT_IN"):
        MinuteInputs(real_input, **{**ACCEPT, flag: False})


def test_real_preflight_preserves_unknowns_and_observed_quantities(real_input, real_doc):
    data = MinuteInputs(real_input, **ACCEPT)
    assert len(data.bars) == 924 and data.research
    report = data.preflight()
    assert report["ready"] and not report["strict_execution_admitted"]
    assert not report["historical_pit_verified"]
    assert not report["research"]["original_admission"]["ready_for_execution"]
    assert not report["action_coverage"]["absence_proved"]
    for row in real_doc["minute"]:
        assert row["available_at"] is row["bar_start"] is row["bar_end"] is None
    for state in real_doc["states"]:
        assert state["reference_price"] is state["upper_limit"] is state["lower_limit"] is None
        assert state["available_at"] is None
    assert data.document == real_doc  # consumer does not rewrite facts into model clocks


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda d: d["research"].update(strict_execution_admitted=True), "PROFILE_REQUIRED"),
        (lambda d: d["research"]["assumptions"].pop(), "PROFILE_REQUIRED"),
        (lambda d: d["securities"].append("000002.XSHE"), "FIXED_CASE_SCOPE"),
        (
            lambda d: d["research"]["original_admission"].update(ready_for_execution=True),
            "CANNOT_PROMOTE",
        ),
        (
            lambda d: d["research"]["tdx_reports"]["600000.XSHG"].update(interval_verified=True),
            "SOURCE_FLAGS",
        ),
        (lambda d: d["minute"].pop(), "BAR_COVERAGE_GAP"),
        (lambda d: d["minute"].append(d["minute"][0]), "DUPLICATE_BAR"),
        (lambda d: d["minute"][0].update(volume="100.5"), "VOLUME_NOT_SHARES"),
        (lambda d: d["minute"][0].update(volume="0"), "ZERO_VOLUME_HALT_AMBIGUITY"),
        (
            lambda d: d["minute"][0].update(bar_start="2026-10-09T09:35:00+08:00"),
            "UNKNOWN_BAR_CLOCKS",
        ),
        (
            lambda d: d["minute"][0].update(available_at="2026-10-09T09:36:00+08:00"),
            "UNKNOWN_BAR_CLOCKS",
        ),
        (
            lambda d: d["minute"][0].update(replay_bar_start="2026-10-09T09:34:00+08:00"),
            "INTERVAL_MODEL",
        ),
        (lambda d: d["minute"][0].update(high="10.08"), "PRICE_ENVELOPE_TOUCHED"),
        (lambda d: d["minute"][0].update(low="9.32"), "PRICE_ENVELOPE_TOUCHED"),
        (lambda d: d["states"][0].update(upper_limit="10.67"), "UNKNOWN_LIMITS"),
        (lambda d: d["states"][0].update(reference_price="9.70"), "UNKNOWN_LIMITS"),
        (lambda d: d["states"][0].update(previous_close="9.71"), "STATE_PROJECTION"),
        (
            lambda d: d["states"][0]["tdx_previous_close"].update(close="9.71"),
            "PREVIOUS_CLOSE_MISMATCH",
        ),
        (lambda d: d["states"][0].update(suspended=None), "EXPLICIT_STATE"),
        (
            lambda d: d["states"][0].update(replay_effective_at="2026-10-08T09:00:00+08:00"),
            "SAME_DAY_0900",
        ),
        (lambda d: d["action_coverage"].update(absence_proved=True), "BOUNDED_ACTION_EVIDENCE"),
        (
            lambda d: d["action_coverage"]["public_action_query"]["rows"].append(
                {"ex_date": "2026-10-12"}
            ),
            "KNOWN_ACTION",
        ),
        (
            lambda d: d["action_coverage"]["public_action_query"]["linked_events"][0].update(
                payment_date="2026-10-12"
            ),
            "KNOWN_ACTION",
        ),
        (
            lambda d: d["action_coverage"]["public_action_query"]["source_queries"][0].update(
                source_query_complete=False
            ),
            "INCOMPLETE_QUERY",
        ),
        (
            lambda d: d["action_coverage"]["tdx_actions"][0]["rows"][0].update(
                year=2026, month=10, day=12
            ),
            "KNOWN_ACTION",
        ),
    ],
)
def test_real_input_faults_fail_before_any_execution(real_doc, tmp_path, mutate, error):
    mutate(real_doc)
    path = tmp_path / "fault.json"
    path.write_text(json.dumps(real_doc))
    with pytest.raises(ValueError, match=error):
        MinuteInputs(path, **ACCEPT)


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda d: d["minute"][0].update(amount="0"), "MINUTE_INVALID_NUMBER"),
        (
            lambda d: d["minute"][0]["evidence"].update(manifest_sha256="wrong-source"),
            "BAR_SOURCE_BINDING",
        ),
        (
            lambda d: d["states"][0]["evidence"].update(binding_sha256="wrong-state-package"),
            "STATE_SOURCE_BINDING",
        ),
        (
            lambda d: d["action_coverage"]["public_action_query"].update(milestones_checked=[]),
            "ACTION_MILESTONES",
        ),
    ],
)
def test_projected_source_identity_and_required_action_dates(real_doc, tmp_path, mutate, error):
    mutate(real_doc)
    path = tmp_path / "fault.json"
    path.write_text(json.dumps(real_doc))
    with pytest.raises(ValueError, match=error):
        MinuteInputs(path, **ACCEPT)


@pytest.mark.parametrize(
    "name,expected",
    [("shared_cash_t1", Decimal("99818.055")), ("visible_momentum", Decimal("100252.0624"))],
)
def test_real_scripts_economics_and_determinism(real_input, tmp_path, name, expected):
    demo = runpy.run_path(str(EXAMPLES / "run_demo.py"))
    results = [
        run_backtest(
            EXAMPLES / (name + ".py"),
            real_input,
            tmp_path / suffix,
            config=demo["CONFIG"],
            **ACCEPT,
        )["result"]
        for suffix in ("a", "b")
    ]
    assert results[0] == results[1]
    result = results[0]
    audit = demo["economic_audit"](result, real_input)
    assert Decimal(audit["cash_from_trade_reconciliation"]) == expected
    assert result["real_market_data"] and result["real_strategy_executed"]
    assert result["native_classes"]["account"] == "rqalpha.portfolio.account.Account"
    assert result["native_classes"]["broker"].endswith("SimulationBroker")
    config = json.loads((tmp_path / "a/native-config.json").read_text())
    assert config["mod"]["sys_simulation"]["price_limit"]
    assert config["mod"]["sys_simulation"]["volume_limit"]
    assert all(
        config["mod"]["sys_risk"][k]
        for k in ("validate_price", "validate_cash", "validate_is_trading")
    )
    assert config["mod"]["sys_accounts"]["stock_t1"]
    assert not run_backtest(
        EXAMPLES / (name + ".py"), real_input, tmp_path / "a", config=demo["CONFIG"], **ACCEPT
    )["native_run"]


@pytest.mark.parametrize(
    "operation,error",
    [
        ("get_current_data()['600000.XSHG'].high_limit", "EXCHANGE_PRICE_LIMIT_UNKNOWN"),
        ("get_current_data()['600000.XSHG'].low_limit", "EXCHANGE_PRICE_LIMIT_UNKNOWN"),
        ("get_current_data()['600000.XSHG'].day_open", "DAY_OPEN_OUTSIDE_RESEARCH_SESSION"),
        ("order('600000.XSHG', 100, LimitOrderStyle(10.09))", "PRICE_ENVELOPE_TOUCHED"),
        ("order('600000.XSHG', 100)", "MUST_EXIT_WITHIN_ACTION_SCOPE"),
    ],
)
def test_unknown_fields_envelope_and_open_ended_positions_fail(
    real_input, tmp_path, operation, error
):
    script = tmp_path / "failure.py"
    script.write_text(
        "from jqdata import *\n"
        "def initialize(context): run_daily(go, '09:40')\n"
        f"def go(context): {operation}\n"
    )
    with pytest.raises(MinuteRunError, match=error):
        run_backtest(script, real_input, tmp_path / "run", **ACCEPT)
    assert not (tmp_path / "run/complete.json").exists()


def test_real_volume_partial_fills_day_expiry_and_lunch_clock(real_input, tmp_path):
    script = tmp_path / "partial.py"
    script.write_text("""from jqdata import *
def initialize(context):
    g.day=0
    run_daily(go, '09:40')
    run_daily(cancel, '09:42')
def before_trading_start(context): g.day+=1
def go(context):
    if g.day==1:
        g.o=order('600000.XSHG', 2000, LimitOrderStyle(9.80))
        order('000001.XSHE', 100, LimitOrderStyle(11.35))
    else:
        order_target('600000.XSHG', 0, LimitOrderStyle(9.40))
def cancel(context):
    if g.day==1:
        assert get_orders(g.o.order_id)[g.o.order_id].filled==1600
        assert cancel_order(g.o)
def after_trading_end(context):
    assert not get_open_orders()
""")
    root = tmp_path / "run"
    result = run_backtest(script, real_input, root, config={"volume_percent": 0.001}, **ACCEPT)[
        "result"
    ]
    buys = [t for t in result["trades"] if t["side"] == "BUY"]
    assert [(t["at"][11:16], t["amount"]) for t in buys] == [("09:41", 900), ("09:42", 700)]
    assert not result["final_account"]["positions"]
    assert any(
        o["amount"] == 2000 and o["filled"] == 1600 and o["status"] == "canceled"
        for o in result["orders"]
    )
    assert any(
        o["security"] == "000001.XSHE" and o["filled"] == 0 and o["status"] == "rejected"
        for o in result["orders"]
    )
    events = [json.loads(s) for s in (root / "events.jsonl").read_text().splitlines()]
    assert not any("11:30" < e["at"][11:16] < "13:00" for e in events)
