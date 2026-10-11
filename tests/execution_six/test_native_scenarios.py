"""Native behavior checks over explicit synthetic inputs; zero real-case claims."""

import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from importlib import metadata
from pathlib import Path

import pytest

from ashare_quant.execution_six.contracts import CASES, event_plan

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ('import json,sys; p,a=json.loads(sys.argv[1]);sys.path[:0]=p;'
             'sys.argv=["fixture",*a];from fixture_worker import main;main()')


def invoke(case, target, variant="normal", ok=True, *, b12_control=None):
    try:
        site = str(metadata.distribution("rqalpha").locate_file(""))
    except metadata.PackageNotFoundError:
        pytest.skip("Native RQAlpha dependency required; this is not a real-case result")
    args = [case, str(target), "--variant", variant]
    if b12_control is not None:
        args += ["--b12-control", b12_control]
    paths = [str(ROOT / "src"), site, str(Path(__file__).parent)]
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
    process = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", BOOTSTRAP,
                              json.dumps([paths, args])], env=env, capture_output=True, text=True, timeout=60)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix(".stdout").write_text(process.stdout)
    target.with_suffix(".stderr").write_text(process.stderr)
    if ok:
        assert process.returncode == 0, process.stderr[-6000:]
    else:
        assert process.returncode != 0
    return process


def snapshot(result, label):
    return next(x for x in result["snapshots"] if x["label"] == label)


def test_single_session_without_fabricated_adjacent_dates(tmp_path):
    target = tmp_path / "bounded"
    invoke("B09", target, variant="single_session")
    result = json.loads((target / "result.json").read_text())
    manifest = json.loads((target / "manifest.json").read_text())
    events = [json.loads(line) for line in (target / "events.jsonl").read_text().splitlines()]
    assert result["scenario_triggered"] and not result["actual_real_case_run"]
    assert manifest["bootstrap"]["calendar"] == ["2024-03-04"]
    assert any(e["kind"] == "unused_previous_session" and e["value"] is None for e in events)
    assert any(e["kind"] == "terminal_lifecycle_model" and e["next_session"] is None for e in events)
    assert all(r["operation"] != "previous_close" for r in result["owner_reads"])


@pytest.mark.parametrize("case", ["B09", "B08", "B10", "B13", "B11", "B12"])
def test_actual_native_fixture_behavior(case, tmp_path):
    path = tmp_path / case
    invoke(case, path)
    result = json.loads((path / "result.json").read_text())
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    assert result["status"] == "SYNTHETIC_SCENARIO_EXECUTED"
    assert result["verification_only"] and not result["actual_real_case_run"]
    assert result["submissions"] == 1
    assert result["cross_platform_comparison"] == "NOT_PERFORMED"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert result["native_classes"]["broker"] == "rqalpha.mod.rqalpha_mod_sys_simulation.simulation_broker.SimulationBroker"
    assert result["native_classes"]["portfolio"] == "rqalpha.portfolio.Portfolio"
    assert snapshot(result, "initial")["account"]["positions"] == []
    assert Decimal(result["final_account"]["frozen_cash"]) == 0
    assert all(x["logical_at"].endswith("+08:00") for x in result["owner_reads"])
    assert all(x["operation"] != "capacity" for x in result["owner_reads"] if x["consumer"] == "strategy")
    assert json.loads((path / "teardown.json").read_text())["native_singleton_cleared"]
    if case == "B11":
        assert result["trades"] == []
        assert Decimal(result["final_account"]["cash"]) == 1000000
        assert result["final_account"]["positions"] == []
        assert any(e["kind"] == "order_request" for e in events)
        assert any(e["kind"] == "native_order_event" and e["event"] == "ORDER_CREATION_REJECT" for e in events)
        return
    filled = 2300 if case in {"B10", "B13"} else 100
    fill_price = Decimal("8.00") if case == "B12" else Decimal("20.10")
    final_price = Decimal("8.30") if case == "B12" else Decimal("20.30")
    assert sum(t["quantity"] for t in result["trades"]) == filled
    assert {Decimal(t["price"]) for t in result["trades"]} == {fill_price}
    assert all(Decimal(t["commission"]) == Decimal(t["tax"]) == 0 for t in result["trades"])
    final = result["final_account"]
    assert Decimal(final["cash"]) == Decimal(CASES[case].initial_cash) - filled * fill_price
    assert Decimal(final["total_value"]) == Decimal(final["cash"]) + filled * final_price
    assert final["positions"][0]["quantity"] == filled
    assert snapshot(result, "end_of_day")["account"]["positions"][0]["sellable"] == 0
    if case == "B08":
        assert Decimal(result["orders"][0]["limit_price"]) == Decimal("20.30")
    if case in {"B10", "B13"}:
        order = result["orders"][0]
        assert order["requested"] == 100000 and order["remaining"] == 97700
        assert order["native_status"] == "CANCELLED"
        assert any(e["kind"] == "capacity_read" and e["volume"] == 23456000 for e in events)
        if case == "B10":
            assert Decimal(snapshot(result, "before_cancel")["account"]["frozen_cash"]) > 0
            assert Decimal(snapshot(result, "after_cancel")["account"]["frozen_cash"]) == 0
            assert snapshot(result, "before_cancel")["open_order_ids"]
            assert snapshot(result, "after_cancel")["open_order_ids"] == []
            assert any(e["kind"] == "cancel_return" for e in events)
        else:
            assert not any(e["kind"] == "cancel_request" for e in events)
    if case == "B12":
        assert snapshot(result, "after_submit")["at"].endswith("09:20:00+08:00")
        assert Decimal(snapshot(result, "after_submit")["account"]["frozen_cash"]) == 1100
        checkpoints = [s for s in result["snapshots"] if s["label"] == "opening_checkpoint"]
        assert [sum(t["quantity"] for t in s["trades"]) for s in checkpoints] == [0, 0, 0, 100, 100]
        assert result["trades"][0]["at"].endswith("09:30:00+08:00")
        assert not result["independent_auction_matching"]
        # Opening is an explicit quote/capacity pulse, not an invented minute bar.
        assert not any(r["operation"] == "bar" and r["logical_at"].endswith("09:30:00+08:00") for r in result["owner_reads"])
    else:
        request_index = next(i for i, e in enumerate(events) if e["kind"] == "order_request")
        fill_index = next(i for i, e in enumerate(events) if e["kind"] == "native_trade")
        returned_index = next(i for i, e in enumerate(events) if e["kind"] == "order_return")
        assert request_index < fill_index < returned_index


def test_completed_reentry_never_places_second_order(tmp_path):
    path = tmp_path / "reentry"
    invoke("B09", path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir() if p.is_file()}
    process = invoke("B09", path)
    assert json.loads(process.stdout)["native_run"] is False
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir() if p.is_file()}
    changed = invoke("B09", path, variant="zero_capacity", ok=False)
    assert "SIX_COMPLETED_IDENTITY_MISMATCH" in changed.stderr
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir() if p.is_file()}
    (path / "config.json").write_text("{}")
    tampered = invoke("B09", path, ok=False)
    assert "SIX_COMPLETED_ARTIFACT_MISMATCH" in tampered.stderr


def test_missing_source_preserves_failure_and_cannot_complete(tmp_path):
    path = tmp_path / "missing"
    invoke("B09", path, variant="missing_bar", ok=False)
    failure = json.loads((path / "failure.json").read_text())
    assert "SYNTHETIC_OWNER_BAR_UNAVAILABLE" in failure["message"]
    assert not (path / "complete.json").exists()
    again = invoke("B09", path, variant="missing_bar", ok=False)
    assert "SIX_INCOMPLETE_ATTEMPT_RETAINED" in again.stderr


def test_zero_capacity_is_not_a_successful_partial_fill(tmp_path):
    path = tmp_path / "zero"
    invoke("B13", path, variant="zero_capacity")
    result = json.loads((path / "result.json").read_text())
    assert result["status"] == "SYNTHETIC_SCENARIO_NOT_TRIGGERED"
    assert not result["scenario_triggered"] and result["trades"] == []
    assert Decimal(result["final_account"]["cash"]) == 100000000


def test_exact_case_clock_scope():
    plan = event_plan(CASES["B12"])
    assert [x["index"] for x in plan] == list(range(1, 11))
    assert [x["at"][11:16] for x in plan] == ["09:00", "09:20", "09:25", "09:28", "09:30", "09:31", "09:32", "10:01", "15:30", "23:59"]
    assert all(x["at"].endswith("+08:00") for x in plan)


@pytest.mark.parametrize("case,variant,error", [("B13", "invalid_capacity", "SIX_OWNER_VOLUME_TYPE"),
                                                 ("B12", "invalid_opening", "SIX_OWNER_PRICE_TYPE")])
def test_unknown_or_coerced_native_inputs_fail_before_a_fill(case, variant, error, tmp_path):
    path = tmp_path / variant
    invoke(case, path, variant=variant, ok=False)
    assert error in json.loads((path / "failure.json").read_text())["message"]
    assert not (path / "complete.json").exists()
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    assert not any(e["kind"] == "native_trade" for e in events)
