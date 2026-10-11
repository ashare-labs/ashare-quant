"""Recompute current per-goal review from sealed original runs; never executes JQ."""

import json
from collections import Counter
from pathlib import Path

from ashare_quant.round_one.compare import read_json_line, sha
from ashare_quant.round_one.goal_compare import correct_sell_price_fields, terminal_review

ROOT = Path(__file__).resolve().parents[1]


def generate(root=ROOT, *, write=True, version="dev7"):
    if version not in {"dev7", "dev8"}:
        raise ValueError(version)
    docs = root / "docs/round-one"
    matrix_name = "status-matrix.json" if version == "dev7" else f"status-matrix-{version}.json"
    matrix = json.loads((docs / matrix_name).read_text())
    bindings = {
        c["case_id"]: c for c in json.loads((docs / "input-bindings.json").read_text())["cases"]
    }
    policy = json.loads((docs / f"goal-policy-{version}.json").read_text())
    out = docs / "goal-results"
    if write:
        out.mkdir(exist_ok=True)
    cases = []
    for item in matrix["cases"]:
        cid = item["case_id"]
        binding = bindings[cid]
        rule = policy["cases"][cid]
        row = dict(
            case_id=cid,
            primary_verdict=rule["verdict"],
            primary_goal=rule["goal"],
            explanation=rule["explanation"],
            limits=rule["limits"],
            minimum_additional_evidence=rule.get("minimum_additional_evidence"),
            source_case_sha256=binding["files"].get("case.md"),
            execution_status=item["status"],
            new_jq_runs=0,
        )
        native = item.get("native_report")
        if native:
            report = json.loads(Path(native).read_text())
            run = Path(report["aq_run_root"])
            folder = Path(binding["folder"])
            assert sha(folder / "events.jsonl") == binding["files"]["events.jsonl"]
            assert sha(folder / "jq.py") == binding["files"]["jq.py"]
            complete = json.loads((run / "complete.json").read_text())
            assert sha(run / "complete.json") == report["aq_complete_sha256"]
            for name, checksum in complete["artifacts"].items():
                assert sha(run / name) == checksum
            jq = [read_json_line(x) for x in (folder / "events.jsonl").read_text().splitlines()]
            aq = [read_json_line(x) for x in (run / "events.jsonl").read_text().splitlines()]
            reviewed = correct_sell_price_fields(report, jq, aq)
            reviewed["schema"] = "aq.round-one.goal-comparison.v1"
            reviewed["basis_report"] = dict(path=native, sha256=sha(native))
            reviewed["terminal_review"] = terminal_review((folder / "jq.py").read_text(), jq, aq)
            assert (
                reviewed["terminal_review"]["verdict"]
                == "FIXED_TERMINAL_READOUT_NO_ORDERING_EFFECT"
            ), cid
            classification = Counter(r["classification"] for r in reviewed["differences"])
            buckets = {
                "primary_material": [],
                "strategy_observable_but_not_decision_input_in_frozen_case": [],
                "terminal_readout_scope": [],
                "reference_insufficient": [],
            }
            for label, count in classification.items():
                if label in {"NATIVE_CASH_RESERVATION_POLICY", "UNPROVEN_JQ_EX_MARK_ROUNDING"}:
                    key = "primary_material"
                elif label in {
                    "OPEN_ORDER_QUERY_PHASE_SEMANTICS",
                    "LUNCH_PENDING_ORDER_QUERY_VIEW",
                }:
                    key = "strategy_observable_but_not_decision_input_in_frozen_case"
                elif (
                    label.startswith("TERMINAL_")
                    or label == "OBSERVED_TERMINAL_WITHDRAWABLE_PRESENTATION"
                ):
                    key = "terminal_readout_scope"
                else:
                    raise ValueError((cid, label))
                buckets[key].append(dict(classification=label, field_rows=count))
            reviewed["impact_buckets"] = buckets
            target = (
                docs / "reservation-goal-results" if version == "dev8" and cid == "B10" else out
            )
            if write:
                target.mkdir(exist_ok=True)
            p = target / (cid + ".json")
            encoded = json.dumps(reviewed, ensure_ascii=False, indent=2, default=str) + "\n"
            if write and (version == "dev7" or cid == "B10"):
                p.write_text(encoded)
            else:
                assert p.read_text() == encoded, cid
            row.update(
                report=dict(path=str(p), sha256=sha(p)),
                differences_by_impact=buckets,
                corrected_order_field_count=len(reviewed["order_field_corrections"]),
                terminal_clock_verdict=reviewed["terminal_review"]["verdict"],
            )
        if item.get("query_report"):
            q = Path(item["query_report"])
            row["query_report"] = dict(path=str(q), sha256=sha(q))
        cases.append(row)
    assert set(policy["cases"]) == set(bindings) == {r["case_id"] for r in cases}
    counts = dict(Counter(r["primary_verdict"] for r in cases))
    result = dict(
        schema="aq.round-one.exclusive-primary-goal-review.v1",
        case_count=len(cases),
        counts=counts,
        classification_rule=policy["classification_rule"],
        common_limits=policy["common_limits"],
        cases=cases,
        new_jq_runs=0,
        native_cases=sum("native_report" in r for r in matrix["cases"]),
        categories_are_mutually_exclusive=True,
        previous_overlapping_review_retained="difference-review-dev6.json",
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if write:
        (docs / f"goal-review-{version}.json").write_text(encoded)
    else:
        assert (docs / f"goal-review-{version}.json").read_text() == encoded
    return result


if __name__ == "__main__":
    print(json.dumps(generate()["counts"], ensure_ascii=False))
