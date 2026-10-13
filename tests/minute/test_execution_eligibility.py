"""Independent price eligibility and zero-capacity regressions, never a JQ proof."""

import copy
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ashare_quant.minute import research
from ashare_quant.minute.inputs import MinuteInputs
from ashare_quant.minute.runner import MinuteRunError
from examples.jq_minute_comparison.audit_execution import audit_run
from examples.jq_minute_comparison.audit_gaps import TDX_SHA256

SID = "600000.XSHG"
JQ = dict(
    lifecycle="jq_minute",
    matching="jq_current",
    capacity_policy="jq_daily_initial",
    volume_percent=0.0001,
)


@pytest.mark.parametrize("api", ["order", "order_value", "order_target", "order_target_value"])
def test_opening_price_eligibility_precedes_all_native_order_sizing(
    run_minute, minute_session_doc, tmp_path, api
):
    for row in minute_session_doc["session_observations"]["opening_prices"]:
        row["available_at"] = row["date"] + "T10:00:00+08:00"
    source = f"from jqdata import *\ndef initialize(c):run_daily(buy,'09:30')\ndef buy(c):{api}('{SID}',1000)"
    with pytest.raises(MinuteRunError, match="JQ_OPENING_EXECUTION_PRICE_UNAVAILABLE"):
        run_minute(source, doc=minute_session_doc, config=JQ)
    root = tmp_path / "run"
    events = [json.loads(s) for s in (root / "events.jsonl").read_text().splitlines()]
    assert not any(e["kind"] in {"order_request", "order_event", "trade"} for e in events)
    assert not (root / "complete.json").exists()


def test_visible_opening_executes_at_its_own_price_and_closed_bar_needs_no_day_open(
    run_minute, minute_session_doc
):
    # The opening scalar is different from yesterday and from the first close.
    for state in minute_session_doc["states"]:
        state["previous_close"] = "9.5" if state["security"] == SID else state["previous_close"]
    source = """from jqdata import *
def initialize(c):
    run_daily(buy,'09:30');run_daily(later,'09:31')
def buy(c):
    if str(c.current_dt.date())=='2024-03-04': order('600000.XSHG',100)
def later(c):
    if str(c.current_dt.date())=='2024-03-05':
        try:get_current_data()['600000.XSHG'].day_open
        except ValueError:pass
        else:raise AssertionError('delayed day_open unexpectedly visible')
        order('600000.XSHG',100)
"""
    for row in minute_session_doc["session_observations"]["opening_prices"]:
        if row["date"] == "2024-03-05":
            row["available_at"] = row["date"] + "T10:00:00+08:00"
    result, _, root = run_minute(source, doc=minute_session_doc, config=JQ)
    assert [(t["at"][11:16], t["price"]) for t in result["trades"]] == [
        ("09:30", 10),
        ("09:31", 10.11),
    ]
    audit_run(root)


@pytest.mark.parametrize("visible", [False, True])
def test_resume_uses_only_current_day_visible_morning_bars(run_minute, minute_session_doc, visible):
    for row in minute_session_doc["minute"]:
        if (
            row["bar_end"].startswith("2024-03-04")
            and row["bar_end"][11:16] <= "11:30"
            and not visible
        ):
            row["available_at"] = "2024-03-04T13:01:00+08:00"
    source = """from jqdata import *
def initialize(c):run_daily(buy,'13:00')
def buy(c):
    if str(c.current_dt.date())=='2024-03-04':order('600000.XSHG',100)
"""
    if not visible:
        with pytest.raises(MinuteRunError, match="JQ_RESUME_EXECUTION_PRICE_UNAVAILABLE"):
            run_minute(source, doc=minute_session_doc, config=JQ)
    else:
        result, _, root = run_minute(source, doc=minute_session_doc, config=JQ)
        assert len(result["trades"]) == 1 and result["trades"][0]["price"] == 10.129
        audit_run(root)


def test_zero_minute_keeps_callbacks_and_pending_orders_but_has_no_fills(
    run_minute, minute_session_doc
):
    for bar in minute_session_doc["minute"]:
        if bar["security"] == SID and bar["bar_end"][11:16] == "14:59":
            bar.update(volume="0", amount="0")
    for row in minute_session_doc["session_observations"]["daily"]:
        if row["security"] == SID:
            row["volume"] = "2390000"
    source = """from jqdata import *
def initialize(c):
    run_daily(rest,'14:58');run_daily(zero,'14:59')
def rest(c):
    if str(c.current_dt.date())=='2024-03-04':g.old=order('600000.XSHG',1000,LimitOrderStyle(10.5))
def zero(c):
    if str(c.current_dt.date())!='2024-03-04':return
    assert attribute_history('600000.XSHG',1,'1m',['volume'],fq=None).volume.iloc[0]==0
    g.market=order('600000.XSHG',100)
    assert g.market.filled==0 and g.market.status==OrderStatus.canceled
    g.new=order('600000.XSHG',100,LimitOrderStyle(10.5))
    assert g.new.filled==0
def handle_data(c,data):
    if str(c.current_dt.date())=='2024-03-04' and c.current_dt.strftime('%H:%M')=='14:59':
        assert get_orders(g.old.order_id)[g.old.order_id].filled==200
        assert get_orders(g.new.order_id)[g.new.order_id].filled==0
def after_trading_end(c):
    if str(c.current_dt.date())=='2024-03-04':
        assert get_orders(g.old.order_id)[g.old.order_id].filled==201
        assert get_orders(g.new.order_id)[g.new.order_id].filled==1
"""
    result, events, root = run_minute(source, doc=minute_session_doc, config=JQ)
    assert [(t["at"][11:16], t["amount"]) for t in result["trades"]] == [
        ("14:58", 200),
        ("15:00", 1),
        ("15:00", 1),
    ]
    assert sum(e["kind"] == "clock" and e["phase"] == "bar" for e in events) == 720
    caps = [
        e
        for e in events
        if e["kind"] == "volume_capacity" and e.get("stage") == "zero_volume_minute"
    ]
    assert len(caps) == 3 and all(e["per_order_cap"] == 0 for e in caps)
    assert result["execution_policy"]["unverified_jq_rules"]
    audit_run(root)


def test_frozen_false_opening_profit_is_rejected_by_independent_auditor():
    path = os.environ.get("AQ_R5_DELAYED_OPEN_EVIDENCE")
    if not path:
        pytest.skip("Frozen R5 independent delayed-opening failure artifact required")
    result = json.loads((Path(path) / "result.json").read_text())
    assert result["total_pnl"] == 95 and result["trades"][0]["price"] == 9.5
    with pytest.raises(ValueError, match="fill without visible opening execution price"):
        audit_run(path)


def test_frozen_initial_fill_at_zero_minute_is_rejected_by_independent_auditor():
    path = os.environ.get("AQ_R5_ZERO_FILL_EVIDENCE")
    if not path:
        pytest.skip("Frozen R5 zero-minute initial-fill artifact required")
    result = json.loads((Path(path) / "result.json").read_text())
    assert len(result["trades"]) == 1 and result["trades"][0]["at"][11:16] == "14:59"
    with pytest.raises(ValueError, match="fill at zero-volume minute"):
        audit_run(path)


@pytest.fixture
def zero_capture():
    raw = os.environ.get("AQ_JQ_GAP_EVIDENCE")
    base = os.environ.get("AQ_TDX_RESEARCH_INPUT")
    if not raw or not base:
        pytest.skip("Frozen AD raw-zero source and old research input required")
    path = Path(raw) / "tdx-full-days.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == TDX_SHA256
    return json.loads(path.read_text()), json.loads(Path(base).read_text())


def zero_doc(capture, index):
    data, base = capture
    zeros = [
        (sid, blob, row)
        for sid, blob in data.items()
        for row in blob["rows"]
        if row["source_label"][:10] in base["days"] and float(row["volume"]) == 0
    ]
    assert len(zeros) == 4
    sid, blob, raw = zeros[index]
    doc = copy.deepcopy(base)
    doc.update(schema=research.SCHEMA_FULL)
    doc["research"].update(profile=research.PROFILE_FULL, assumptions=research.FULL_ASSUMPTIONS)
    doc["sessions"] = {
        d: [
            [d + "T" + a + ":00+08:00", d + "T" + b + ":00+08:00"]
            for a, b in research.FULL_SESSIONS
        ]
        for d in doc["days"]
    }
    end = datetime.fromisoformat(raw["source_label"])
    row = dict(
        security=sid,
        source_label=end.isoformat(),
        bar_start=None,
        bar_end=None,
        available_at=None,
        historical_available_at=None,
        replay_bar_end=end.isoformat(),
        replay_bar_start=(end - timedelta(minutes=1)).isoformat(),
        **{f: raw[f] for f in ["open", "high", "low", "close", "volume"]},
        amount=raw["money"],
        evidence={
            "source": "tdx_native_0x052d",
            "manifest_sha256": blob["report"]["manifest_sha256"],
        },
    )
    # Test-only declaration of the *required* AD role, not an AD delivery.
    row.update(
        session_phase="closing_auction",
        phase_evidence=dict(
            model="ad-declared-closing-stage.v1",
            source_label=row["source_label"],
            manifest_sha256=row["evidence"]["manifest_sha256"],
            historical_phase_verified=False,
            source_refs=["test-only conditional role: pending the AD owner's actual export"],
        ),
    )
    doc["minute"] = [row]
    return doc


@pytest.mark.parametrize("index", range(4))
def test_each_real_zero_row_passes_activity_only_with_role_and_still_requires_full_coverage(
    zero_capture, index, tmp_path
):
    doc = zero_doc(zero_capture, index)
    row = doc["minute"][0]
    assert float(row["volume"]) == float(row["amount"]) == 0
    path = tmp_path / "isolated-admission.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="MINUTE_BAR_COVERAGE_GAP"):
        MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)
    # The source row is accepted without filling or altering zeroes. This
    # intentionally incomplete input must never become executable evidence.
    row.pop("phase_evidence")
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="MINUTE_RESEARCH_ZERO_VOLUME_PHASE_REQUIRED"):
        MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda r: r.update(session_phase="continuous"), "ZERO_VOLUME_PHASE_REQUIRED"),
        (lambda r: r.update(amount="1"), "ZERO_VOLUME_AMOUNT_CONFLICT"),
        (lambda r: r.update(volume="1"), "MINUTE_INVALID_NUMBER"),
        (
            lambda r: r["phase_evidence"].update(manifest_sha256="wrong"),
            "ZERO_VOLUME_PHASE_EVIDENCE",
        ),
        (
            lambda r: r["phase_evidence"].update(source_label="2026-10-09T14:58:00+08:00"),
            "ZERO_VOLUME_PHASE_EVIDENCE",
        ),
        (
            lambda r: r["phase_evidence"].update(historical_phase_verified=True),
            "ZERO_VOLUME_PHASE_EVIDENCE",
        ),
        (lambda r: r["phase_evidence"].update(source_refs=[]), "ZERO_VOLUME_PHASE_EVIDENCE"),
    ],
)
def test_zero_admission_does_not_waive_role_provenance_or_nonzero_turnover(
    zero_capture, tmp_path, mutation, error
):
    doc = zero_doc(zero_capture, 0)
    mutation(doc["minute"][0])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=error):
        MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)


def test_declared_closing_role_does_not_admit_continuous_zero_or_change_v1(zero_capture, tmp_path):
    doc = zero_doc(zero_capture, 0)
    row = doc["minute"][0]
    end = datetime.fromisoformat(row["source_label"]).replace(hour=10, minute=0)
    row.update(
        source_label=end.isoformat(),
        replay_bar_end=end.isoformat(),
        replay_bar_start=(end - timedelta(minutes=1)).isoformat(),
    )
    row["phase_evidence"]["source_label"] = row["source_label"]
    path = tmp_path / "continuous-zero.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="ZERO_VOLUME_PHASE_REQUIRED"):
        MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)
    assert research.ASSUMPTIONS[3] not in research.FULL_ASSUMPTIONS
    with pytest.raises(ValueError, match="ZERO_VOLUME_HALT_AMBIGUITY"):
        research.validate_activity(row, 0, 0, full=False)
