"""Real-input and real-platform evidence checks; absent private evidence is a skip."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from examples.jq_minute_comparison.align import NAMES, captures, compare_aligned
from examples.jq_minute_comparison.audit_execution import audit_run


@pytest.fixture
def alignment_evidence():
    path = os.environ.get("AQ_MINUTE_ALIGNMENT_RESULTS")
    if not path:
        pytest.skip("Supply frozen native policy matrix; no fabricated JQ observations")
    return Path(path)


@pytest.mark.parametrize("name", NAMES)
def test_same_real_jq_reference_now_matches_all_fills_and_after_order_accounts(
    alignment_evidence, name
):
    source = os.environ.get("AQ_JQ_MINUTE_EVIDENCE")
    if not source:
        pytest.skip("Real JQ capture is required")
    report = compare_aligned(
        name,
        alignment_evidence / (name + "-jq_current-resize"),
        captures(Path(source), name),
        must_align=True,
    )
    assert report["aligned_fill_and_after_order_checks"]
    assert report["native_audit"]["capacity_violations"] == []
    assert not report["strict_equivalence"]
    assert report["remaining"]


@pytest.mark.parametrize("mutation", ["cash", "closeable", "fee", "capacity", "result_trade"])
def test_independent_audit_rejects_inconsistent_native_evidence(
    alignment_evidence, tmp_path, mutation
):
    root = tmp_path / "run"
    shutil.copytree(alignment_evidence / "shared_cash_t1-jq_current-resize", root)
    rows = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()]
    trade = next(e for e in rows if e["kind"] == "trade")
    account = next(e for e in rows if e["kind"] == "account" and e["positions"])
    if mutation == "cash":
        account["available_cash"] += 1
    elif mutation == "closeable":
        account["positions"]["600000.XSHG"]["closeable_amount"] += 100
    elif mutation == "fee":
        trade["commission"] += 1
    elif mutation == "capacity":
        trade["amount"] = 1000000000
    else:
        result = json.loads((root / "result.json").read_text())
        result["trades"][0]["trade_id"] = "altered"
        (root / "result.json").write_text(json.dumps(result))
    (root / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    # Bypass the byte-hash check deliberately to exercise the economic audit.
    complete = json.loads((root / "complete.json").read_text())
    complete["artifacts"] = {
        n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in complete["artifacts"]
    }
    (root / "complete.json").write_text(json.dumps(complete))
    with pytest.raises(ValueError):
        audit_run(root)
