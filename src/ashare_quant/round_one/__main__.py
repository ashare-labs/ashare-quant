"""Read-only comparison of six existing runs against frozen first-round inputs."""

import argparse
import json
from collections import Counter
from pathlib import Path

from .compare import compare_d1, read_json, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--aq-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bindings, audit = read_json(args.bindings), read_json(args.aq_audit)
    args.output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for binding in bindings["cases"]:
        case_id = binding["case_id"]
        if case_id not in audit["cases"]:
            continue
        result = compare_d1(binding, audit["cases"][case_id]["path"])
        result["comparison_source_sha256"] = sha(Path(__file__).with_name("compare.py"))
        target = args.output / (case_id + ".json")
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        summaries.append(
            dict(
                case_id=case_id,
                status=result["status"],
                fields=len(result["comparisons"]),
                differences=len(result["differences"]),
                classes=dict(Counter(r.get("classification") for r in result["differences"])),
                unresolved=result["unresolved_count"],
                report=str(target.resolve()),
                sha256=sha(target),
            )
        )
    output = dict(
        schema="aq.round-one.dimension-comparison.v1",
        dimension="D1",
        bindings_sha256=sha(args.bindings),
        aq_audit_sha256=sha(args.aq_audit),
        cases=summaries,
        aq_reruns=0,
        jq_reruns=0,
    )
    (args.output / "summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
