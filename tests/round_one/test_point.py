import json
from decimal import Decimal

import pytest
from test_fees import invoke

from ashare_quant.round_one.point_cases import CASES
from ashare_quant.round_one.point_host import PointSource
from ashare_quant.round_one.point_owner import PointOwner


def test_lunch_order_waits_for_native_point_pulse_without_any_bar(tmp_path):
    output = tmp_path / "B20"
    run = invoke("B20", output)
    assert run.returncode == 0, run.stderr[-7000:]
    result = json.loads((output / "result.json").read_text())
    assert result["scenario_triggered"] and result["actual_real_case_run"] is False
    assert result["no_ohlc_synthesis"] and result["strategy_point_access"] is False
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert (trade["at"], trade["quantity"], Decimal(trade["price"])) == (
        "2024-03-04T13:01:00+08:00",
        100,
        Decimal("20.50"),
    )
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    snapshots = {e["label"]: e for e in events if e["kind"] == "snapshot"}
    for label in ["after_order", "probe_12:59", "probe_13:00"]:
        s = snapshots[label]
        assert s["trades"] == [] and s["orders"][0]["filled"] == 0
        assert Decimal(s["account"]["frozen_cash"]) == Decimal("2020")
    assert snapshots["probe_13:01"]["orders"][0]["filled"] == 0
    assert snapshots["probe_15:00"]["orders"][0]["filled"] == 100
    assert Decimal(result["final_account"]["cash"]) == Decimal("997950")
    assert Decimal(snapshots["probe_15:00"]["account"]["positions_value"]) == Decimal("2030")
    assert not any(e["kind"] == "owner_read" and e["operation"] == "bar" for e in events)
    first_point = next(
        e["seq"] for e in events if e["kind"] == "owner_read" and e["operation"] == "point"
    )
    assert first_point > snapshots["probe_13:00"]["seq"]
    fill_seq = next(e["seq"] for e in events if e["kind"] == "native_trade")
    assert fill_seq > snapshots["probe_13:01"]["seq"]


def test_missing_point_fails_without_completed_seal_or_fabricated_fill(tmp_path):
    output = tmp_path / "missing"
    run = invoke("B20", output, "missing_point")
    assert run.returncode != 0 and not (output / "complete.json").exists()
    assert (
        json.loads((output / "failure.json").read_text())["message"]
        == "POINT_REQUIRED_OBSERVATION_UNAVAILABLE"
    )
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert not any(e["kind"] == "native_trade" for e in events)


def test_point_role_and_clock_cannot_bypass_future_boundary():
    owner = object.__new__(PointOwner)
    owner.case = CASES["B20"]
    owner.points = None
    for clock, consumer in [("13:01", "strategy"), ("12:00", "engine"), ("13:00", "engine")]:
        with pytest.raises(RuntimeError, match="POINT_ENGINE_CLOCK_ROLE_DENIED"):
            owner.read("point", owner.case.at(clock), consumer=consumer)
    with pytest.raises(RuntimeError, match="POINT_MODEL_HAS_NO_OHLC_BAR"):
        object.__new__(PointSource).get_bar(None, None, None)
