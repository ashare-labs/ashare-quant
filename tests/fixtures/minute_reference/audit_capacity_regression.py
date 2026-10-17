"""Audit a frozen lunch-session minute-capacity regression in a new output directory."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from tests.fixtures.minute_reference.align import execute, sha
from tests.fixtures.minute_reference.audit_execution import audit_run
from tests.fixtures.minute_reference.compare import INPUT_SHA256, near, number, require


def trade_changes(before, after):
    by_clock = [defaultdict(list), defaultdict(list)]
    for target, result in zip(by_clock, (before, after), strict=True):
        for trade in result["trades"]:
            target[trade["at"], trade["security"], trade["side"]].append(
                {k: v for k, v in trade.items() if k != "trade_id"}
            )
    return [
        dict(
            at=key[0], security=key[1], side=key[2], before=by_clock[0][key], after=by_clock[1][key]
        )
        for key in sorted(set(by_clock[0]) | set(by_clock[1]))
        if by_clock[0][key] != by_clock[1][key]
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", required=True, type=Path, help="read-only frozen lunch-session capacity case directory")
    p.add_argument("--before", required=True, type=Path, help="original frozen native run")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path, help="new writable evidence directory")
    p.add_argument("--accept-research", action="store_true")
    args = p.parse_args()
    if not args.accept_research:
        p.error("--accept-research must accept the existing TDX profile's bounded assumptions")
    spec = json.loads((args.case / "manifest.json").read_text())
    finding = json.loads((args.case / "cap-finding.json").read_text())
    require(spec["id"] == "R09-lunch-participation", "this verifier expects frozen R09")
    require(sha(args.input) == spec["data"]["input_sha256"] == INPUT_SHA256, "R09 input changed")
    strategy = args.case / "strategy.py"
    require(sha(strategy) == spec["strategy_sha256"], "R09 strategy changed")
    require(
        sha(args.before / "result.json") == finding["result_sha256"], "R09 baseline result changed"
    )
    require(
        sha(args.before / "events.jsonl") == finding["events_sha256"], "R09 baseline events changed"
    )
    require(sha(args.before / "strategy.py") == sha(strategy), "baseline ran a different strategy")
    require(
        sha(args.before / "input.json") == INPUT_SHA256, "baseline ran a different market input"
    )
    before_manifest = json.loads((args.before / "manifest.json").read_text())
    require(
        all(before_manifest["config"][k] == v for k, v in spec["config"].items()),
        "baseline config changed",
    )
    before_audit = audit_run(args.before, enforce_strict_cap=False)
    require(before_audit["capacity_violations"], "reported baseline violation was not reproduced")
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ("strategy.py", "manifest.json", "cap-finding.json"):
        (args.output / ("original-" + name)).write_bytes((args.case / name).read_bytes())
    result = execute(strategy, args.input, args.output / "after", spec["config"])
    after_audit = audit_run(args.output / "after")
    before = json.loads((args.before / "result.json").read_text())
    require(
        not result["final_account"]["positions"] and not before["final_account"]["positions"],
        "R09 final holdings not flat",
    )
    cash_difference = number(after_audit["exact_cash"]) - number(before_audit["exact_cash"])
    near(
        number(result["final_account"]["total_value"])
        - number(before["final_account"]["total_value"]),
        cash_difference,
        "R09 economic difference not reconciled",
    )
    report = dict(
        schema="aq-minute-r09-capacity-fix.v1",
        case_id=spec["id"],
        input_sha256=INPUT_SHA256,
        strategy_sha256=sha(strategy),
        baseline_artifacts={
            name: sha(args.before / name)
            for name in ("result.json", "events.jsonl", "complete.json")
        },
        before_audit=before_audit,
        after_audit=after_audit,
        before_final=before["final_account"],
        after_final=result["final_account"],
        final_difference=str(cash_difference),
        changed_trade_slots=trade_changes(before, result),
        completed_reentry_native_run=False,
        default_capacity_fixed=True,
        jq_equivalence=False,
        new_platform_runs=0,
        original_case_modified=False,
    )
    (args.output / "capacity-fix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    # Verify read-only baselines again after the native child has finished.
    require(
        sha(args.before / "result.json") == finding["result_sha256"]
        and sha(args.before / "events.jsonl") == finding["events_sha256"],
        "baseline changed during run",
    )
    print(
        json.dumps(
            dict(
                before_trades=len(before["trades"]),
                after_trades=len(result["trades"]),
                before_cash=before_audit["exact_cash"],
                after_cash=after_audit["exact_cash"],
                difference=str(cash_difference),
                violations_after=after_audit["capacity_violations"],
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
