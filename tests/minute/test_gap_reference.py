"""Real partial-fill evidence and failures that must not become equivalence claims."""

import copy
import json
import os
from decimal import getcontext, localcontext
from pathlib import Path

import pytest

from tests.fixtures.minute_reference.audit_gaps import audit_old_gaps, audit_trace, load_capture


@pytest.fixture
def evidence():
    root = os.environ.get("AQ_JQ_GAP_EVIDENCE")
    if not root:
        pytest.skip("Complete real JQ gap capture required; no fabricated platform result")
    root = Path(root)
    return load_capture(root), json.loads((root / "tdx-full-days.json").read_text())


def test_real_partial_fills_cash_reservations_and_t1(evidence):
    rows, source = evidence
    report = audit_trace(rows, source)
    assert report["accounts_checked"] == 48
    assert len(report["trades"]) == 9
    assert report["final_cash"] == "9508475.786960"
    assert report["final_quantities"] == {"600000.XSHG": 37912, "000001.XSHE": 10700}
    assert [c["amount"] for c in report["capacity_checks"]] == [
        9400,
        9400,
        10700,
        9400,
        10,
        89,
        9400,
        86,
        127,
    ]
    assert [
        c["aq_whole_lot_cap"] for c in report["capacity_checks"] if "aq_whole_lot_cap" in c
    ] == [0, 0, 0, 100]
    assert len(report["volume_differences"]) == 12
    assert all(
        r["handle_count"] == 240 and r["scheduled_without_handle"] == ["11:30", "12:00", "15:00"]
        for r in report["lifecycle"]
    )
    assert report["expiry_observation"]["exact_cancel_time_observed"] is False
    assert report["strict_equivalence"] is False


def test_audit_is_independent_of_ambient_decimal_precision(evidence):
    with localcontext() as context:
        context.prec = 8
        report = audit_trace(*evidence)
        assert report["final_cash"] == "9508475.786960"
        assert getcontext().prec == 8


@pytest.mark.parametrize(
    "kind",
    [
        "cash",
        "locked",
        "quantity",
        "t1",
        "cost",
        "mark",
        "order_time",
        "order_fee",
        "future_trade",
        "orphan_trade",
        "duplicate_trade",
        "partial_quantity",
        "partial_price",
        "future_history",
        "daily_volume",
        "clock_list",
        "schedule_clock",
        "schedule_count",
        "terminal",
        "sequence",
    ],
)
def test_partial_trace_rejects_causal_and_intermediate_mutations(evidence, kind):
    rows, source = copy.deepcopy(evidence)
    r = next(
        r
        for r in rows
        if r["kind"] == "account"
        and r.get("label") == "scheduled:13:01"
        and r["time"].startswith("2026-10-09")
    )
    if kind == "cash":
        r["account"]["available_cash"] += 1
        r["account"]["total_value"] += 1
    elif kind == "locked":
        r["account"]["available_cash"] -= 1
        r["account"]["locked_cash"] += 1
    elif kind == "quantity":
        r["positions"][0]["total_amount"] += 1
    elif kind == "t1":
        r["positions"][0]["closeable_amount"] += 1
    elif kind == "cost":
        r["positions"][0]["avg_cost"] += 0.01
    elif kind == "mark":
        p = r["positions"][0]
        p["price"] += 0.01
        delta = p["total_amount"] * 0.01
        p["value"] += delta
        r["account"]["positions_value"] += delta
        r["account"]["total_value"] += delta
    elif kind == "order_time":
        r["orders"][0]["add_time"] = "2026-10-09 14:00:00"
    elif kind == "order_fee":
        r["orders"][0]["commission"] += 1
    elif kind in {"future_trade", "orphan_trade", "duplicate_trade"}:
        if kind == "future_trade":
            r["trades"][0]["time"] = "2026-10-09 14:00:00"
        elif kind == "orphan_trade":
            r["trades"][0]["order_id"] = -1
        else:
            r["trades"].append(copy.deepcopy(r["trades"][0]))
    elif kind in {"partial_quantity", "partial_price"}:
        r = next(
            r
            for r in rows
            if r["kind"] == "account" and any(t["amount"] == 10 for t in r["trades"])
        )
        t = next(t for t in r["trades"] if t["amount"] == 10)
        t["amount" if kind == "partial_quantity" else "price"] += 1
    elif kind == "future_history":
        next(r for r in rows if r["kind"] == "market")["securities"]["600000.XSHG"]["history"][0][
            "label"
        ] = "2026-10-09 15:00:00"
    elif kind == "daily_volume":
        next(r for r in rows if r["kind"] == "closed_daily")["rows"][0]["volume"] += 1000000
    elif kind == "clock_list":
        next(r for r in rows if r["kind"] == "after_trading")["handle_clocks"][119] = "11:30"
    elif kind in {"schedule_clock", "schedule_count"}:
        r = next(r for r in rows if r["kind"] == "scheduled")
        r["clock" if kind == "schedule_clock" else "handle_count"] = (
            "09:31" if kind == "schedule_clock" else 1
        )
    elif kind == "terminal":
        rows.pop()
    else:
        rows[5]["seq"] += 1
    with pytest.raises(ValueError):
        audit_trace(rows, source)


def test_old_mark_and_sample_clock_gaps_are_explained_offline(evidence):
    jq = os.environ.get("AQ_JQ_MINUTE_EVIDENCE")
    matrix = os.environ.get("AQ_MINUTE_ALIGNMENT_RESULTS")
    if not jq or not matrix:
        pytest.skip("Original real captures and native matrix required")
    report = audit_old_gaps(matrix, jq, evidence[1])
    for name, expected in (("shared_cash_t1", 190), ("visible_momentum", 136)):
        case = report[name]
        assert float(case["valuation_delta"]) == expected
        assert float(case["residual"]) == 0
        assert case["old_checkpoint_counts"] == dict(aq=22, jq=24, common=20)
        assert len(case["aq_only"]) == 2
        assert len(case["jq_only"]) == 4
