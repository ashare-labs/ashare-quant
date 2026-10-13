"""Frozen AD/JQ evidence checks; absent external artifacts are explicit skips."""

import copy
import json
import os
from decimal import localcontext
from pathlib import Path

import pytest

from ashare_quant.minute.inputs import MinuteInputs, stamp
from examples.jq_minute_comparison.align import captures
from examples.jq_minute_comparison.audit_execution import audit_run
from examples.jq_minute_comparison.audit_gaps import load_capture
from examples.jq_minute_comparison.session_compare import (
    CASES,
    compare_case,
    compare_rows,
    observations,
    verify_source_observations,
)
from examples.jq_minute_comparison.session_input import (
    INHERITED,
    project,
    verify_delivery,
)


def evidence(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip("Real external artifact required: " + name)
    return Path(value)


@pytest.fixture(scope="module")
def real_document():
    package = evidence("AQ_AD_SESSION_PACKAGE")
    assert verify_delivery(package) == 141
    return project(package / "session-source.json", evidence("AQ_TDX_RESEARCH_INPUT"))


def load_document(document, tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(document))
    return MinuteInputs(path, accept_assumed=True, accept_state_replay=True, accept_research=True)


def test_every_ad_value_role_and_unknown_clock_is_preserved(real_document, tmp_path):
    original = json.loads((evidence("AQ_AD_SESSION_PACKAGE") / "session-source.json").read_bytes())
    base = json.loads(evidence("AQ_TDX_RESEARCH_INPUT").read_bytes())
    assert all(real_document[k] == base[k] for k in INHERITED)
    assert real_document["session_observations"] == original["session_observations"]
    for actual, raw in zip(real_document["minute"], original["minute"], strict=True):
        assert {k: actual[k] for k in raw} == raw
        assert all(
            actual[k] is None
            for k in ("bar_start", "bar_end", "available_at", "historical_available_at")
        )
    data = load_document(real_document, tmp_path)
    assert len(data.bars) == 960 and len(data.session_data.history) == 480
    assert sum(float(r["volume"]) == 0 for r in real_document["minute"]) == 4
    assert (
        sum(float(r["volume"]) == 0 for r in original["session_observations"]["history_minutes"])
        == 2
    )


def test_real_opening_and_daily_knowledge_clocks(real_document, tmp_path):
    data = load_document(real_document, tmp_path)
    for sid in data.ids:
        for day in data.days:
            obs = data.session_data
            with pytest.raises(ValueError, match="DAY_OPEN_NOT_YET_VISIBLE"):
                obs.visible_open(sid, stamp(day + "T09:29:59+08:00"))
            assert obs.visible_open(sid, stamp(day + "T09:30:00+08:00")) > 0
            assert obs.visible_daily(sid, stamp(day + "T14:59:59+08:00"), day, day) == []
            daily = obs.visible_daily(sid, stamp(day + "T15:00:00+08:00"), day, day)
            assert len(daily) == 1
            bars = [b for b in real_document["minute"] if b["security"] == sid and b["date"] == day]
            assert float(daily[0]["volume"]) == sum(float(b["volume"]) for b in bars)
            assert daily[0]["close"] == bars[-1]["close"]


@pytest.mark.parametrize(
    "mutation",
    ["missing_role", "wrong_source", "certified_phase", "missing_row", "positive_amount"],
)
def test_real_zero_row_cannot_be_deleted_repaired_or_detached(real_document, tmp_path, mutation):
    doc = copy.deepcopy(real_document)
    row = next(r for r in doc["minute"] if float(r["volume"]) == 0)
    if mutation == "missing_role":
        row.pop("phase_evidence")
    elif mutation == "wrong_source":
        row["phase_evidence"]["manifest_sha256"] = "0" * 64
    elif mutation == "certified_phase":
        row["phase_evidence"]["historical_phase_verified"] = True
    elif mutation == "missing_row":
        doc["minute"].remove(row)
    else:
        row["amount"] = "1"
    with pytest.raises(ValueError, match="MINUTE_"):
        load_document(doc, tmp_path)


@pytest.mark.parametrize("name", CASES)
def test_real_three_scripts_and_all_recorded_fields(name):
    result = compare_case(
        name,
        evidence("AQ_FULL_SESSION_RUNS") / name,
        evidence("AQ_JQ_MINUTE_EVIDENCE"),
        evidence("AQ_JQ_GAP_EVIDENCE"),
    )
    assert result["unexplained_differences"] == 0
    assert result["zero_volume_fills"] == 0
    assert result["native_audit"]["both_cash_components_checked"]


def test_real_comparison_does_not_depend_on_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 3
        report = compare_case(
            "shared_cash_t1",
            evidence("AQ_FULL_SESSION_RUNS") / "shared_cash_t1",
            evidence("AQ_JQ_MINUTE_EVIDENCE"),
            evidence("AQ_JQ_GAP_EVIDENCE"),
        )
        assert context.prec == 3
    assert report["aq_final"]["total_value"] == 99793.5454


@pytest.mark.parametrize(
    "mutation",
    [
        "cash_offset",
        "order_status",
        "order_quantity",
        "trade_price",
        "callback_clock",
        "terminal_collection",
    ],
)
def test_full_observer_comparison_rejects_economic_and_lifecycle_mutations(mutation):
    root = evidence("AQ_FULL_SESSION_RUNS") / "shared_cash_t1"
    rows = observations(root, "shared_cash_t1")
    jq = captures(evidence("AQ_JQ_MINUTE_EVIDENCE"), "shared_cash_t1")
    if mutation == "cash_offset":
        row = next(r for r in rows if r["kind"] == "snapshot")
        row["account"]["available_cash"] -= 1
        row["account"]["locked_cash"] += 1
    elif mutation.startswith("order_"):
        row = next(r for r in rows if r["kind"] == "order_return" and r["order"])
        row["order"]["status" if mutation == "order_status" else "filled"] = (
            "filled" if mutation == "order_status" else 1
        )
    elif mutation == "trade_price":
        row = next(r for r in rows if r["kind"] == "snapshot" and r["trades"])
        row["trades"][0]["price"] += 0.01
    elif mutation == "callback_clock":
        rows[1]["time"] = "2026-10-09 09:01:00"
    else:
        rows[-2]["orders"] = next(
            r["orders"] for r in rows if r["kind"] == "snapshot" and r["orders"]
        )
    with pytest.raises(ValueError, match="Observation"):
        compare_rows(rows, jq)


@pytest.mark.parametrize("field", ["volume", "close", "label"])
def test_source_difference_allowance_does_not_allow_fabricated_aq_history(real_document, field):
    rows = observations(evidence("AQ_FULL_SESSION_RUNS") / "gap_ported", "gap_ported")
    row = next(r for r in rows if r["kind"] == "market")
    bar = row["securities"]["600000.XSHG"]["history"][0]
    bar[field] = "2026-10-13 15:00:00" if field == "label" else bar[field] + 1
    with pytest.raises(ValueError, match="AD history differs|Future observed minute"):
        verify_source_observations(rows, real_document)


def test_gap_volume_differences_remain_explicit():
    rows = observations(evidence("AQ_FULL_SESSION_RUNS") / "gap_ported", "gap_ported")
    jq = load_capture(evidence("AQ_JQ_GAP_EVIDENCE"))
    report = compare_rows(rows, jq)
    assert report["difference_counts"]["unmodified_TDX_JQ_source_volume_difference"] == 27
    assert report["unexplained_differences"] == 0


def test_real_zero_new_and_pending_orders_and_opening_visibility():
    root = evidence("AQ_REAL_BOUNDARY_RUN")
    result = json.loads((root / "result.json").read_bytes())
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert result["real_market_data"] and result["jq_comparison"] == "NOT_PERFORMED"
    assert len(result["orders"]) == len(result["trades"]) == 12
    assert not any(t["at"][11:16] == "14:59" for t in result["trades"])
    records = [e["values"] for e in events if e["kind"] == "record"]
    assert records == [
        {"real_zero_rows_checked": 2},
        {"real_zero_rows_checked": 4},
        {"real_boundary_complete": 1},
    ]
    assert audit_run(root)["both_cash_components_checked"]
