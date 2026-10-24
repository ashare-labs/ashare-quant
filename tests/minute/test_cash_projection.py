"""COMP-03: cash decomposition must follow each native order and fill."""

import copy
import hashlib
import json
import os
import shutil
from decimal import getcontext, localcontext
from pathlib import Path

import pytest

from tests.fixtures.minute_reference.align import captures, compare_aligned
from tests.fixtures.minute_reference.audit_execution import audit_run

POLICIES = [(m, c) for m in ("next_open", "jq_current") for c in ("reject", "resize")]


def rewrite(root, events):
    path = root / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    receipt = json.loads((root / "complete.json").read_text())
    receipt["artifacts"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "complete.json").write_text(json.dumps(receipt))


@pytest.mark.parametrize("matching,cash_policy", POLICIES)
def test_real_cash_projection_cannot_shift_into_a_nonexistent_reserve(
    tmp_path, matching, cash_policy
):
    jq = os.environ.get("AQ_JQ_MINUTE_EVIDENCE")
    matrix = os.environ.get("AQ_MINUTE_ALIGNMENT_RESULTS")
    if not jq or not matrix:
        pytest.skip("Real captures and native matrix required")
    root = tmp_path / "probe"
    shutil.copytree(Path(matrix) / f"shared_cash_t1-{matching}-{cash_policy}", root)
    rows = [json.loads(s) for s in (root / "events.jsonl").read_text().splitlines()]
    for event in rows:
        if event["kind"] != "log" or not event["message"].startswith("AQMIN_JSON "):
            continue
        row = json.loads(event["message"].removeprefix("AQMIN_JSON "))
        if (
            row["kind"] == "snapshot"
            and row["label"].startswith("handle_data:")
            and row["positions"]
            and row["account"]["locked_cash"] == 0
            and row["account"]["available_cash"] > 100
        ):
            row["account"]["available_cash"] -= 100
            row["account"]["locked_cash"] += 100
            event["message"] = "AQMIN_JSON " + json.dumps(row)
            break
    else:
        raise AssertionError("Post-fill observer checkpoint missing")
    rewrite(root, rows)
    with pytest.raises(ValueError, match="frozen cash mismatch"):
        compare_aligned(
            "shared_cash_t1",
            root,
            captures(Path(jq), "shared_cash_t1"),
            must_align=(matching, cash_policy) == ("jq_current", "resize"),
        )


SOURCE = """from aquant import *
import json
def snapshot(c, label):
    g.seq += 1
    p = c.portfolio
    log.info('AQMIN_JSON ' + json.dumps(dict(
        kind='snapshot', seq=g.seq, time=str(c.current_dt), label=label,
        account={k:getattr(p,k) for k in ['available_cash','locked_cash','total_value','positions_value']},
        positions=[vars(v) for v in p.positions.values()], orders=None, trades=None)))
def initialize(c):
    g.day=0; g.seq=0
    run_daily(submit,'09:31')
    run_daily(partial,'09:32')
    run_daily(cancel,'09:33')
    run_daily(expire,'14:58')
def before_trading_start(c):
    g.day+=1
    snapshot(c,'before')
def submit(c):
    if g.day == 1:
        g.order=order('600000.XSHG',1000,LimitOrderStyle(10.5))
        snapshot(c,'submitted')
def partial(c):
    if g.day == 1: snapshot(c,'partially_filled')
def cancel(c):
    if g.day == 1:
        cancel_order(g.order)
        snapshot(c,'cancelled')
def expire(c):
    if g.day == 1:
        order('600000.XSHG',100,LimitOrderStyle(2))
        snapshot(c,'pending_day_end')
def after_trading_end(c): snapshot(c,'day_end')
"""


@pytest.mark.parametrize("matching,cash_policy", POLICIES)
def test_partial_release_cancel_expiry_and_each_projection_are_independently_checked(
    run_minute, minute_doc, tmp_path, matching, cash_policy
):
    for row in minute_doc["minute"]:
        price = 10 + 10 * minute_doc["securities"].index(row["security"])
        row.update(open=str(price), high=str(price), low=str(price), close=str(price))
    result, events, root = run_minute(
        SOURCE, config={"matching": matching, "cash_policy": cash_policy, "volume_percent": 0.01}
    )
    with localcontext() as ctx:
        ctx.prec = 8
        report = audit_run(root)
        assert getcontext().prec == 8
    assert report["both_cash_components_checked"]
    checkpoints = report["observer_checkpoints"]
    for label in ("submitted", "partially_filled", "pending_day_end"):
        assert float(next(r for r in checkpoints if r["label"] == label)["locked_cash"]) > 0
    for label in ("cancelled", "day_end"):
        assert float(next(r for r in checkpoints if r["label"] == label)["locked_cash"]) == 0
    assert result["final_account"]["locked_cash"] == pytest.approx(0, abs=1e-7)

    for label in ("submitted", "partially_filled", "cancelled", "pending_day_end", "day_end"):
        changed = copy.deepcopy(events)
        for event in changed:
            if event["kind"] != "log" or not event["message"].startswith("AQMIN_JSON "):
                continue
            row = json.loads(event["message"].removeprefix("AQMIN_JSON "))
            if row.get("label") == label:
                row["account"]["available_cash"] -= 100
                row["account"]["locked_cash"] += 100
                event["message"] = "AQMIN_JSON " + json.dumps(row)
                break
        target = tmp_path / ("mutated-" + label)
        shutil.copytree(root, target)
        rewrite(target, changed)
        with pytest.raises(ValueError, match="frozen cash mismatch"):
            audit_run(target)
