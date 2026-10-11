"""Independent read-only assertions over the two completed E03 projections and traces."""

import hashlib
import json
import sys
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root):
    root = Path(root)

    def load(name):
        return json.loads((root / name).read_text())

    frozen, left, right = (
        load("input.json"),
        load("control-result.json"),
        load("resumed-result.json"),
    )
    stopped, trace, checks = load("stopped.json"), load("attempt-traces.json"), load("checks.json")
    assert stopped["error"]["returncode"] == -9 and stopped["committed_cursor"] == 2
    assert left["summary"] == right["summary"] and left["account"] == right["account"]
    for key in [
        "decision_id",
        "economic_input_sha256",
        "signal",
        "orders",
        "fills",
        "account_at_day_close",
    ]:
        assert [d[key] for d in left["days"]] == [d[key] for d in right["days"]], key
    decisions = [d["decision_id"] for d in right["days"]]
    orders = [o for d in right["days"] for o in d["orders"]]
    fills = [f for d in right["days"] for f in d["fills"]]
    assert len(set(decisions)) == len(decisions) == 10
    assert len(orders) == len(fills) == 1
    assert orders[0]["side"] == "buy" and orders[0]["requested_quantity"] == 100
    assert fills[0]["order_id"] == orders[0]["order_id"]
    submitted = [e for e in trace["attempt-1.trace.jsonl"] if e["kind"] == "native_submit_return"]
    assert len(submitted) == 1 and submitted[0]["details"]["decision_id"] == decisions[0]
    assert submitted[0]["details"]["order_id"] == orders[0]["order_id"]
    resumed = trace["attempt-2.trace.jsonl"]
    restored = [e for e in resumed if e["kind"] == "restored_native_state"]
    assert len(restored) == 1 and restored[0]["cursor"] == 2
    assert restored[0]["details"]["pending_submit"] is False
    assert not any(e["kind"] == "native_submit_return" for e in resumed)
    assert decisions[0] not in [
        e["details"]["decision_id"] for e in resumed if e["kind"] == "decision_candidate"
    ]
    assert checks["completed_reentry_no_native_run"] and checks["owner_unchanged"]
    return dict(
        case_id="E03",
        status="ENGINEERING_SCENARIO_VERIFIED",
        source_commit="377524a",
        source_sha256=frozen["source_sha256"],
        dataset_id=frozen["dataset_id"],
        owner_version=right["data_identity"]["owner_version"]
        if "owner_version" in right["data_identity"]
        else "0.8.0.dev2",
        root=str(root.resolve()),
        job_id=frozen["job"]["job_id"],
        boundary=frozen["boundary"],
        fault=frozen["fault"],
        prepared_same_session=True,
        interrupt_returncode=-9,
        durable_cursor=2,
        equal_decision_ids=decisions,
        equal_order_ids=[o["order_id"] for o in orders],
        equal_fill_ids=[f["fill_id"] for f in fills],
        first_submitted_decision_not_repeated=True,
        first_order_and_fill=right["days"][0]["orders"] + right["days"][0]["fills"],
        equal_final_account=right["account"],
        completed_reentry_no_native_run=True,
        real_case_evidence_kind="single_end_native_recovery_with_existing_conditional_daily_owner_product",
        jq_comparison="NOT_APPLICABLE",
        jq_new_runs=0,
        limitations=[
            "Daily bounded M2 execution, not minute paper/live recovery",
            "Atomic local event acknowledgement, not external broker exactly-once guarantee",
            "Existing owner quality/assumptions retained; no new market authenticity or PIT certification",
            "Only the declared durable confirmation boundary, not all crash locations",
        ],
        artifacts={
            n: sha(root / n)
            for n in [
                "input.json",
                "control-result.json",
                "stopped.json",
                "resumed-result.json",
                "attempt-traces.json",
                "checks.json",
                "executed-runner.py",
            ]
        },
    )


if __name__ == "__main__":
    print(json.dumps(audit(sys.argv[1]), ensure_ascii=False, indent=2))
