import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from ashare_quant.execution.reservation import budget_remaining


def invoke(tmp_path, scenario):
    out = tmp_path / "result.json"
    paths = [
        str(Path(__file__).resolve().parents[2]),
        str(Path(__file__).resolve().parents[2] / "src"),
        str(metadata.distribution("rqalpha").locate_file("")),
        str(Path(__file__).parent),
    ]
    boot = "import sys,json;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=a;from budget_fixture import main;main()"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            boot,
            json.dumps([paths, ["budget", "--out", str(out), "--scenario", scenario]]),
        ],
        env=dict(
            PATH=os.defpath, TZ="Asia/Shanghai", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-10000:]
    return json.loads(out.read_text())


def test_two_native_fills_fees_concurrent_order_cancel_and_replay(tmp_path):
    r = invoke(tmp_path, "multiple")
    p = {p["kind"]: p for p in r["probes"]}
    assert r["classes"]["account"].endswith("reservation.BudgetAccount")
    assert len(r["trades"]) == 2
    assert sum(t["commission"] for t in r["trades"]) == 5
    assert p["09:31"] == dict(kind="09:31", cash=11610, frozen=7385, total=18995)
    assert p["09:32"] == dict(kind="09:32", cash=11610, frozen=6385, total=17995)
    assert p["cancel"] == dict(kind="cancel", cash=17995, frozen=0, total=17995)
    assert r["state"]["quantity"] == 200


def test_full_native_fill_releases_price_improvement_and_all_reservation(tmp_path):
    r = invoke(tmp_path, "full")
    p = r["probes"][0]
    assert p == dict(kind="09:31", cash=18995, frozen=0, total=18995)
    assert len(r["trades"]) == 1


def test_native_fee_aware_admission_is_not_weakened(tmp_path):
    r = invoke(tmp_path, "fee_rejection")
    assert r["trades"] == [] and r["state"]["quantity"] == 0
    assert r["probes"][0] == dict(kind="09:31", cash=1000, frozen=0, total=1000)


@pytest.mark.parametrize(
    "args",
    [
        (0, 10, 0, 0, 0),
        (100, 10, 101, 10, 0),
        (100, 10, -1, 0, 0),
        (100, 0, 0, 0, 0),
        (100, 10, 0, 0, -1),
        (100, "NaN", 0, 0, 0),
    ],
)
def test_invalid_native_reservation_state_refused(args):
    with pytest.raises(RuntimeError):
        budget_remaining(*args)
