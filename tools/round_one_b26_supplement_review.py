"""Review a completed B26 AQ run against the original and frozen supplement."""

import argparse
import json
from pathlib import Path

from ashare_quant.round_one.compare import sha
from ashare_quant.round_one.dividend_tax_compare import compare
from ashare_quant.round_one.premarket_review import read_supplement, review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True)
    parser.add_argument("--supplement-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binding = next(
        b for b in json.loads(args.bindings.read_text())["cases"] if b["case_id"] == "B26"
    )
    folder = Path(binding["folder"])
    for name, expected in binding["files"].items():
        if sha(folder / name) != expected:
            raise ValueError("B26_ORIGINAL_CHANGED:" + name)
    settings = json.loads((folder / "reference.json").read_text())["settings"]
    evidence = read_supplement(args.supplement, args.supplement_sha256, settings)
    result = review(compare(binding, args.run.resolve()), evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "status": result["status"],
        "field_count": len(result["comparisons"]),
        "difference_count": len(result["differences"]),
        "mark_difference_resolved": result["mark_difference_resolved"],
    }))


if __name__ == "__main__":
    main()
