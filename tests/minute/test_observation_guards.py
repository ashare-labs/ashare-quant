"""Causal, intermediate-account rejection probes on real frozen observations."""

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tests.fixtures.minute_reference.align import NAMES, captures, compare_aligned
from tests.fixtures.minute_reference.compare import audit_jq, audit_observed_marks, read_rows

POLICIES = ["next_open-reject", "next_open-resize", "jq_current-reject", "jq_current-resize"]
P2_PROBES = ["checkpoint_cash", "checkpoint_t1", "future_nested_fill"]


@pytest.fixture
def real_evidence():
    jq, matrix = (
        os.environ.get("AQ_JQ_MINUTE_EVIDENCE"),
        os.environ.get("AQ_MINUTE_ALIGNMENT_RESULTS"),
    )
    if not jq or not matrix:
        pytest.skip(
            "Real JQ captures and native policy matrix required; no fabricated platform success"
        )
    return Path(jq), Path(matrix)


def mutate(rows, kind, platform="jq"):
    rows = copy.deepcopy(rows)
    if kind in ("checkpoint_cash", "checkpoint_t1", "checkpoint_cost", "today_amount"):
        row = next(
            r
            for r in rows
            if r["kind"] == "snapshot" and r["label"].startswith("handle_data:") and r["positions"]
        )
        assert row["trades"] is None
        if kind == "checkpoint_cash":
            row["account"]["available_cash"] += 1
            row["account"]["total_value"] += 1
        elif kind == "checkpoint_t1":
            assert row["positions"][0]["closeable_amount"] == 0
            row["positions"][0]["closeable_amount"] = 100
        elif kind == "checkpoint_cost":
            row["positions"][0]["avg_cost"] += 0.01
        else:
            row["positions"][0]["today_amount"] -= 100
    else:
        returned = next(r for r in rows if r["kind"] == "order_return" and r["order"])
        oid = returned["order"]["order_id"]
        if kind == "future_nested_fill":
            from datetime import timedelta

            from tests.fixtures.minute_reference.compare import timestamp

            later = (timestamp(returned["time"]) + timedelta(minutes=8)).isoformat(sep=" ")
            returned["order"]["add_time"] = later
            for row in rows:
                if row["kind"] != "snapshot":
                    continue
                for order in row["orders"] or []:
                    if order["order_id"] == oid:
                        order["add_time"] = later
                for trade in row["trades"] or []:
                    if trade["order_id"] == oid:
                        trade["time" if platform == "jq" else "at"] = later
        elif kind == "return_api":
            returned["api"] = "order"
        elif kind == "return_flag":
            returned["returned_none"] = True
        elif kind == "duplicate_trade":
            row = next(r for r in rows if r["kind"] == "snapshot" and r["trades"])
            row["trades"].append(copy.deepcopy(row["trades"][0]))
        elif kind == "missing_first_trade":
            next(r for r in rows if r["kind"] == "snapshot" and r["trades"])["trades"] = []
    return rows


@pytest.mark.parametrize("name", NAMES)
def test_every_real_jq_snapshot_and_first_fill_is_reconciled(real_evidence, name):
    jq, _ = real_evidence
    rows = captures(jq, name)
    report = audit_jq(rows, with_checks=True)
    assert report["snapshots_checked"] == sum(r["kind"] == "snapshot" for r in rows)
    assert report["no_trade_collection_snapshots"] == 24
    assert len(report["first_trade_observations"]) == len(report["ledger"])


def test_independent_prices_reject_a_self_consistent_but_false_intermediate_mark(real_evidence):
    path = os.environ.get("AQ_JQ_CHECKPOINT_MARKS")
    if not path:
        pytest.skip("Independent expanded AD checkpoint export required")
    jq, _ = real_evidence
    rows = read_rows(jq / "shared_cash_t1.logs.jsonl")
    row = next(
        r
        for r in rows
        if r["kind"] == "snapshot" and r["label"].startswith("handle_data:") and r["positions"]
    )
    p = row["positions"][0]
    p["price"] += 0.01
    delta = p["total_amount"] * 0.01
    p["value"] += delta
    row["account"]["positions_value"] += delta
    row["account"]["total_value"] += delta
    # Cash/inventory remain consistent; only independent prices expose this.
    audit_jq(rows)
    with pytest.raises(ValueError, match="independent snapshot mark mismatch"):
        audit_observed_marks(rows, json.loads(Path(path).read_text()))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(
    "kind",
    [
        "checkpoint_cost",
        "today_amount",
        "return_api",
        "return_flag",
        "duplicate_trade",
        "missing_first_trade",
    ],
)
def test_account_basis_and_call_collection_gaps_are_rejected(real_evidence, name, kind):
    jq, _ = real_evidence
    with pytest.raises(ValueError):
        audit_jq(mutate(read_rows(jq / (name + ".logs.jsonl")), kind))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind", P2_PROBES)
def test_jq_independent_p2_shapes_are_rejected_in_every_native_policy(
    real_evidence, name, policy, kind
):
    jq, matrix = real_evidence
    with pytest.raises(ValueError):
        compare_aligned(
            name,
            matrix / (name + "-" + policy),
            mutate(read_rows(jq / (name + ".logs.jsonl")), kind),
            must_align=policy == "jq_current-resize",
        )


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind", P2_PROBES)
def test_native_observation_checkpoints_and_times_bind_to_native_events(
    real_evidence, tmp_path, name, policy, kind
):
    jq, matrix = real_evidence
    root = tmp_path / "run"
    shutil.copytree(matrix / (name + "-" + policy), root)
    events = read_rows(root / "events.jsonl")
    logs = [e for e in events if e["kind"] == "log" and e["message"].startswith("AQMIN_JSON ")]
    rows = mutate([json.loads(e["message"].removeprefix("AQMIN_JSON ")) for e in logs], kind, "aq")
    for e, row in zip(logs, rows, strict=True):
        e["message"] = "AQMIN_JSON " + json.dumps(row)
    (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    complete = json.loads((root / "complete.json").read_text())
    complete["artifacts"]["events.jsonl"] = hashlib.sha256(
        (root / "events.jsonl").read_bytes()
    ).hexdigest()
    (root / "complete.json").write_text(json.dumps(complete))
    # Rehash deliberately: this tests ledger/clock gates, not a byte checksum.
    with pytest.raises(ValueError):
        compare_aligned(name, root, captures(jq, name), must_align=policy == "jq_current-resize")
