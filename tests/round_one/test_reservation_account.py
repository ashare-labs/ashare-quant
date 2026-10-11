import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest


def invoke(root, checkpoint=None, kill=None, *, scenario="partial", error=None):
    root.mkdir(parents=True, exist_ok=True)
    out = root / "result.json"
    paths = [
        str(Path(__file__).resolve().parents[2] / "src"),
        str(metadata.distribution("rqalpha").locate_file("")),
        str(Path(__file__).parent),
    ]
    args = ["--out", str(out), "--scenario", scenario]
    if checkpoint:
        args += ["--checkpoint", str(checkpoint)]
    env = dict(
        PATH=os.defpath,
        TZ="Asia/Shanghai",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )
    if kill:
        env["PROBE_KILL"] = kill
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['reservation-fixture',*a];from reservation_fixture import main;main()"
    r = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, args])],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    if kill:
        assert r.returncode == -9, r.stderr[-9000:]
        return
    if error:
        assert r.returncode != 0 and error in r.stderr, r.stderr[-9000:]
        assert not out.exists()
        return
    assert r.returncode == 0, r.stderr[-9000:]
    return json.loads(out.read_text())


def test_partial_price_improvement_remains_frozen_until_cancel_without_second_cash_ledger(tmp_path):
    r = invoke(tmp_path)
    p = {x["kind"]: x for x in r["probes"]}
    assert r["evidence_kind"] == "synthetic" and len(r["trades"]) == 1
    assert r["classes"]["account"].endswith("reservation_account.ReservationAccount")
    assert (
        p["partial"]["total_cash"] == 9000
        and p["partial"]["frozen_cash"] == 6490
        and p["partial"]["cash"] == 2510
    )
    assert (
        p["cancelled"]["total_cash"] == 9000
        and p["cancelled"]["frozen_cash"] == 0
        and p["cancelled"]["cash"] == 9000
    )
    assert p["duplicate_cancel"]["account_state"] == p["cancelled"]["account_state"]
    assert r["state"]["quantity"] == 100


@pytest.mark.parametrize("kill", ["after_replace:2", "before_replace:3"])
def test_partial_reservation_native_restart_and_cancel_replay_are_exactly_once(tmp_path, kill):
    control = invoke(tmp_path / "control")
    checkpoint = tmp_path / "checkpoint"
    invoke(tmp_path / "resumed", checkpoint, kill)
    resumed = invoke(tmp_path / "resumed", checkpoint)
    for key in ["state", "probes", "trades", "trace"]:
        assert resumed[key] == control[key], key
    assert invoke(tmp_path / "resumed", checkpoint) == resumed


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("full_fill", "RESERVATION_ONE_PARTIAL_FILL_REQUIRED"),
        ("second_fill", "RESERVATION_ONE_PARTIAL_FILL_ONLY_OR_ID_COLLISION"),
        ("second_order", "RESERVATION_SINGLE_ORDER_ONLY"),
        ("fee", "RESERVATION_ZERO_COST_CONFIG_REQUIRED"),
    ],
)
def test_unsupported_models_refuse_without_success_receipt(tmp_path, scenario, error):
    invoke(tmp_path, scenario=scenario, error=error)
