"""Synthetic boundary checks, not additional historical acceptance cases."""

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from rqalpha.const import ORDER_TYPE
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderCancelled

from ashare_quant.portfolio.native import Matcher, runtime
from ashare_quant.portfolio.participation import strict_lot_capacity
from tests.support.portfolio_reference import generate

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "volume,expected",
    [
        (0, 0),
        (994999, 0),
        (995000, 0),
        (999999, 0),
        (1000000, 100),
        (1994999, 100),
        (1995000, 100),
        (1999999, 100),
        (2000000, 200),
    ],
)
def test_native_matcher_never_rounds_fractional_budget_up(volume, expected, monkeypatch):
    ins = SimpleNamespace(type="CS", board_type="MainBoard", round_lot=100)
    order = SimpleNamespace(
        order_book_id="600000.XSHG", type=ORDER_TYPE.MARKET, quantity=10000, unfilled_quantity=10000
    )
    matcher = Matcher.__new__(Matcher)
    matcher._inactive_limit = False
    matcher._volume_limit = True
    matcher._volume_percent = 0.0001
    matcher._turnover = defaultdict(int)
    matcher._get_bar_volume = lambda *args, **kwargs: volume
    matcher.host = SimpleNamespace(
        phase="EXECUTE",
        day="2020-02-03",
        data=SimpleNamespace(row=lambda *args: {"volume": str(volume)}),
    )
    monkeypatch.setitem(runtime, "manifest", {"execution": {"volume_ratio": "0.0001"}})
    try:
        actual = matcher._get_liquidity_limited_fill(order, ins)
    except OrderCancelled:
        actual = 0
    assert actual == expected
    assert strict_lot_capacity(volume, "0.0001") == expected


def test_exact_cumulative_capacity_is_not_reissued_per_order():
    assert strict_lot_capacity("1999999", "0.0001", consumed=100) == 0
    assert strict_lot_capacity("2000000", "0.0001", consumed=100) == 100
    assert strict_lot_capacity("999999999999999999999", "0.0000000000000000001") == 0


@pytest.mark.parametrize("volume,fill", [(999999, 0), (1999999, 100)])
def test_real_native_account_at_synthetic_rounding_boundary(tmp_path, volume, fill):
    source = REPO / "tests/fixtures/research/portfolio/participation"
    question = tmp_path / "question"
    strategy = question / "daily-equal-weight"
    strategy.mkdir(parents=True)
    for name in ("strategy.py", "config.json"):
        (strategy / name).write_bytes((source / name).read_bytes())
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["windows"][0]["end"] = "2020-02-03"
    (question / "manifest.json").write_text(json.dumps(manifest))
    data = generate(manifest, constant=True)
    for row in data["daily"]:
        row.update(volume=str(volume), suspended=False)
    input_path = tmp_path / "synthetic-input.json"
    input_path.write_text(json.dumps(data))
    out = tmp_path / "native"
    args = [
        sys.executable,
        "-B",
        "-m",
        "tests.support.portfolio_engine",
        "--manifest",
        str(question / "manifest.json"),
        "--input",
        str(input_path),
        "--strategy",
        "daily-equal-weight",
        "--window",
        "participation",
        "--output",
        str(out),
    ]
    p = subprocess.run(
        args,
        env=dict(os.environ, PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1"),
        text=True,
        capture_output=True,
    )
    # The strict cap is fixed; the frozen day-end cancellation promise is still
    # violated by immediate native market-order cancellation and must not pass.
    assert p.returncode == 3, p.stderr
    result = json.loads((out / "result.json").read_text())
    assert [o["filled"] for o in result["orders"]] == [fill] * 4
    assert [t["quantity"] for t in result["trades"]] == ([fill] * 4 if fill else [])
    audit = json.loads((out / "audit.json").read_text())
    assert audit["accounting_passed"] and not audit["passed"]
    assert len(audit["contract_differences"]) == 4
