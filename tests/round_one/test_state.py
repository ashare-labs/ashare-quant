import json

import pytest
from test_fees import invoke

from ashare_quant.round_one.state_host import StateAPI


def test_empty_account_state_survives_native_date_boundary_without_market_reads(tmp_path):
    output = tmp_path / "state"
    run = invoke("B35", output)
    assert run.returncode == 0, run.stderr[-7000:]
    r = json.loads((output / "result.json").read_text())
    assert r["scenario_triggered"] and r["state_counter"] == 2 and not r["actual_real_case_run"]
    assert r["orders"] == r["trades"] == r["owner_reads"] == []
    assert r["calendar_adjacency"] is None and r["holdings_overnight_admitted"] is False
    events = [json.loads(x) for x in (output / "events.jsonl").read_text().splitlines()]
    callbacks = [e for e in events if e["kind"] == "callback"]
    assert [e["state_counter"] for e in callbacks] == [0, 0, 1, 1, 1, 2, 2]
    updates = [e for e in events if e["kind"] == "state_update"]
    assert [e["before"] for e in updates] == [0, 1] and [e["after"] for e in updates] == [1, 2]
    assert len({e["state_created"] for e in updates}) == 1
    assert all(e["previous_date"] is None for e in callbacks)


def test_state_only_facade_cannot_place_order():
    with pytest.raises(RuntimeError, match="STATE_ONLY_ORDER_DENIED"):
        StateAPI(None).order(100)
