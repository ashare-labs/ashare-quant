"""Query phase, native cancellation and callback visibility under real RQAlpha."""

import json
from decimal import Decimal

import pytest
from test_native_scenarios import invoke, snapshot


@pytest.mark.parametrize("control", [None, "keep_0920", "cancel_0928"])
def test_b12_observable_native_lifecycle(control, tmp_path):
    path = tmp_path / (control or "original")
    invoke("B12", path, b12_control=control)
    result = json.loads((path / "result.json").read_text())
    events = [json.loads(x) for x in (path / "events.jsonl").read_text().splitlines()]
    assert result["status"] == "SYNTHETIC_SCENARIO_EXECUTED"
    assert not result["actual_real_case_run"] and not result["independent_auction_matching"]
    pending = [s for s in result["snapshots"] if s["orders"]
               and s["orders"][0]["native_status"] == "ACTIVE"]
    assert pending
    for s in pending:
        assert s["at"][11:16] <= "09:30"
        assert s["orders"][0]["strategy_status"] == "new"
        assert s["orders"][0]["filled"] == 0
        assert s["open_order_ids"] == []
        assert s["native_open_order_ids"] == [s["orders"][0]["order_id"]]
        assert Decimal(s["account"]["frozen_cash"]) == 1100
        assert Decimal(s["account"]["available_cash"]) == 998900
        assert s["trades"] == []
    # The extra clocks are state observations; no new market data is inferred.
    assert not any(r["logical_at"][11:16] in {"09:24", "09:26"} for r in result["owner_reads"])
    assert not any(r["operation"] == "bar" and r["logical_at"][11:16] == "09:30"
                   for r in result["owner_reads"])
    for e in events:
        if e["kind"] == "strategy_open_query":
            s = next(s for s in result["snapshots"] if s["at"] == e["at"]
                     and s["label"].startswith("checkpoint_"))
            assert e["order_ids"] == s["open_order_ids"]
    if control == "cancel_0928":
        canceled = snapshot(result, "after_cancel_09:28")
        assert canceled["orders"][0]["native_status"] == "CANCELLED"
        assert canceled["orders"][0]["strategy_status"] == "canceled"
        assert canceled["orders"][0]["canceled_at"].endswith("09:28:00+08:00")
        assert canceled["orders"][0]["finished_at"] is None
        assert canceled["native_open_order_ids"] == canceled["open_order_ids"] == []
        assert Decimal(canceled["account"]["frozen_cash"]) == 0
        assert Decimal(canceled["account"]["cash"]) == 1000000
        assert not result["trades"] and not result["final_account"]["positions"]
        returned = next(e for e in events if e["kind"] == "cancel_return")
        assert not returned["returned_none"]
        assert returned["native_type"] == "rqalpha.model.order.Order"
        assert returned["returned_order"]["native_status"] == "CANCELLED"
        assert any(e["kind"] == "native_order_event" and e["event"] == "ORDER_CANCELLATION_PASS"
                   for e in events)
        assert not any(r["operation"] in {"opening_price", "opening_volume"} for r in result["owner_reads"])
    else:
        assert len(result["trades"]) == 1 and result["trades"][0]["quantity"] == 100
        assert result["trades"][0]["at"].endswith("09:30:00+08:00")
        visible = next(s for s in result["snapshots"] if s["trades"])
        assert visible["at"].endswith("09:31:00+08:00")
        assert visible["orders"][0]["strategy_status"] == "held"
        assert visible["orders"][0]["finished_at"] == result["trades"][0]["at"]
        assert Decimal(visible["account"]["frozen_cash"]) == 0
        callback_exit = next(e for e in events if e["kind"] == "callback_exit"
                             and e["at"][11:16] == "09:30")
        fill = next(e for e in events if e["kind"] == "native_trade")
        assert callback_exit["seq"] < fill["seq"] < visible["seq"]


def test_unfilled_order_becomes_queryable_after_opening(tmp_path):
    path = tmp_path / "unmarketable"
    invoke("B12", path, variant="opening_unmarketable", b12_control="keep_0920")
    result = json.loads((path / "result.json").read_text())
    before = snapshot(result, "checkpoint_09:30")
    after = snapshot(result, "checkpoint_09:31")
    assert before["open_order_ids"] == []
    assert len(after["open_order_ids"]) == 1
    assert after["open_order_ids"] == after["native_open_order_ids"]
    assert after["orders"][0]["strategy_status"] == "open"
    assert after["orders"][0]["native_status"] == "ACTIVE"
    assert result["trades"] == [] and not result["scenario_triggered"]


def test_control_identity_and_case_scope(tmp_path):
    path = tmp_path / "keep"
    invoke("B12", path, b12_control="keep_0920")
    assert not json.loads(invoke("B12", path, b12_control="keep_0920").stdout)["native_run"]
    changed = invoke("B12", path, b12_control="cancel_0928", ok=False)
    assert "SIX_COMPLETED_IDENTITY_MISMATCH" in changed.stderr
    wrong = invoke("B09", tmp_path / "wrong", b12_control="cancel_0928", ok=False)
    assert "B12_CONTROL_SCOPE_REQUIRED" in wrong.stderr
