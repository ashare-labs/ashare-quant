"""Run original and observed scripts through the existing native minute runner."""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_quant.backtests.minute import run_backtest
from examples.minute_research.run_demo import CONFIG, economic_audit
from tests.fixtures.minute_reference.build_scripts import NAMES, ROOT, generate
from tests.fixtures.minute_reference.compare import INPUT_SHA256, require


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--accept-research", action="store_true")
    args = p.parse_args()
    if not args.accept_research:
        p.error(
            "--accept-research must explicitly accept the TDX example's three bounded assumptions"
        )
    require(
        hashlib.sha256(args.input.read_bytes()).hexdigest() == INPUT_SHA256,
        "source input hash mismatch",
    )
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"input_sha256": INPUT_SHA256, "cases": {}}
    for name in NAMES:
        observed = args.output / f"{name}.aq.py"
        observed.write_text(generate(name, "aq"))
        outputs = []
        for script, suffix in (
            (ROOT / "examples/minute_research" / f"{name}.py", "-original"),
            (observed, ""),
        ):
            run = run_backtest(
                script,
                args.input,
                args.output / (name + suffix),
                config=CONFIG,
                accept_assumed=True,
                accept_state_replay=True,
                accept_research=True,
            )
            outputs.append(run["result"])
            economic_audit(run["result"], args.input)
        keys = (
            "orders",
            "trades",
            "accounts",
            "final_account",
            "total_pnl",
            "total_return",
            "model",
            "historical_pit_verified",
            "is_test_double",
        )
        require(
            all(outputs[0][k] == outputs[1][k] for k in keys),
            "observation changed native economics",
        )
        reentry = run_backtest(
            observed,
            args.input,
            args.output / name,
            config=CONFIG,
            accept_assumed=True,
            accept_state_replay=True,
            accept_research=True,
        )
        require(
            not reentry["native_run"] and reentry["result"] == outputs[1],
            "completed reentry changed",
        )
        report["cases"][name] = {
            "equal_economic_fields": keys,
            "completed_reentry_native_run": False,
            "observed_script_sha256": hashlib.sha256(observed.read_bytes()).hexdigest(),
            "audit": economic_audit(outputs[1], args.input),
        }
    (args.output / "observation-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
