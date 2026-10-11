import json
import os
import subprocess
import sys
from decimal import Decimal
from importlib import metadata
from pathlib import Path

import pytest


def run_fixture(root, mode, checkpoint=None, kill=None):
    root.mkdir(parents=True, exist_ok=True)
    output = root / "result.json"
    paths = [
        str(Path(__file__).resolve().parents[2] / "src"),
        str(metadata.distribution("rqalpha").locate_file("")),
        str(Path(__file__).parent),
    ]
    args = ["--out", str(output), "--mode", mode]
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
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['cash-fixture',*a];from cash_fixture import main;main()"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, args])],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if kill:
        assert result.returncode == -9, result.stderr[-8000:]
        return None
    assert result.returncode == 0, result.stderr[-10000:]
    return json.loads(output.read_text())


def probe(result, kind, at=None):
    return next(x for x in result["probes"] if x["kind"] == kind and (at is None or x["at"] == at))


def equal(value, expected):
    assert Decimal(value) == Decimal(expected)


def test_review_cent_withdrawal_after_native_partial_trade_reserve_cancel(tmp_path):
    result = run_fixture(tmp_path, "withdrawal_tail")
    before = probe(result, "unsettled_only")
    equal(before["unsettled_sale_cash"], "994.5")
    final = probe(result, "whole_cent_withdrawal")
    equal(final["total_cash"], "0")
    equal(final["frozen_cash"], "0")
    equal(final["transferable_cash"], "0")


def test_orphan_reservation_repair_never_ignores_live_order_or_cent_freeze():
    from types import SimpleNamespace

    from ashare_quant.round_one.cash_account import CashAccount

    a = CashAccount.__new__(CashAccount)
    a._total_cash = 994.5
    a._frozen_cash = 2.2737367544323206e-13
    a._env = SimpleNamespace(broker=SimpleNamespace(get_open_orders=lambda: [object()]))
    assert a._unreserved_freeze_residual() == 0
    a._env.broker.get_open_orders = lambda: []
    assert a._unreserved_freeze_residual() == a._frozen_cash
    a._frozen_cash = 0.01
    assert a._unreserved_freeze_residual() == 0
    a._frozen_cash = 1e-10  # below absolute cap, but far larger than eight ULPs here
    assert a._unreserved_freeze_residual() == 0


def test_sale_cash_waits_for_next_admitted_session_and_t1_unlock(tmp_path):
    result = run_fixture(tmp_path, "cash")
    assert result["classes"]["account"].endswith("cash_account.CashAccount")
    assert result["evidence_kind"] == "synthetic" and len(result["trades"]) == 2
    buy = probe(result, "buy")
    equal(buy["total_cash"], "98995")
    equal(buy["transferable_cash"], "98995")
    assert buy["quantity"] == 100 and buy["closable"] == 0
    assert probe(result, "t1_rejected")["quantity"] == 100
    before_sell = probe(result, "before", "2024-01-05T08:30:00")
    assert before_sell["closable"] == 100
    sale = probe(result, "sell")
    equal(sale["total_cash"], "99989.5")
    equal(sale["unsettled_sale_cash"], "994.5")
    equal(sale["transferable_cash"], "98995")
    equal(probe(result, "after", "2024-01-05T15:30:00")["transferable_cash"], "98995")
    monday = probe(result, "before", "2024-01-08T08:30:00")
    equal(monday["unsettled_sale_cash"], "0")
    equal(monday["transferable_cash"], "99989.5")


def test_reservations_cancel_and_reuse_do_not_create_cash(tmp_path):
    result = run_fixture(tmp_path, "reuse")
    equal(probe(result, "sell")["transferable_cash"], "995")
    reserve = probe(result, "reserved")
    assert Decimal(reserve["frozen_cash"]) > 0
    equal(reserve["transferable_cash"], "995")
    equal(probe(result, "cancelled")["frozen_cash"], "0")
    larger = probe(result, "reserved_settled")
    equal(larger["transferable_cash"], larger["available_cash"])
    assert Decimal(larger["transferable_cash"]) < 995
    used = probe(result, "reused")
    equal(used["total_cash"], "984.5")
    equal(used["unsettled_sale_cash"], "0")
    equal(used["transferable_cash"], "984.5")
    assert used["closable"] == 0
    assert probe(result, "before", "2024-01-08T08:30:00")["closable"] == 100


@pytest.mark.parametrize(
    "mode,kill",
    [
        ("partial", "after_replace:11"),
        ("dividend", "after_replace:10"),
        ("dividend_tax", "after_replace:10"),
    ],
)
def test_cash_restrictions_and_native_actions_survive_atomic_restart(tmp_path, mode, kill):
    control = run_fixture(tmp_path / "control", mode)
    checkpoint = tmp_path / "checkpoint"
    run_fixture(tmp_path / "resumed", mode, checkpoint, kill)
    resumed = run_fixture(tmp_path / "resumed", mode, checkpoint)
    for field in ("state", "trades", "probes", "trace"):
        assert resumed[field] == control[field], field
    assert run_fixture(tmp_path / "resumed", mode, checkpoint) == resumed
    if mode == "partial":
        assert len(resumed["trades"]) == 6
        equal(resumed["state"]["total_cash"], "99988.5")
        equal(resumed["state"]["transferable_cash"], "99988.5")
    else:
        at_payment = probe(resumed, "before", "2024-01-05T08:30:00")
        equal(at_payment["total_cash"], "99051" if mode == "dividend_tax" else "99065")
        equal(at_payment["transferable_cash"], "99051" if mode == "dividend_tax" else "99065")
        equal(at_payment["receivable"], "0")
        equal(at_payment["average"], "9.3")
        assert at_payment["closable"] == 100
