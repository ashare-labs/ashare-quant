"""python -m ashare_quant.backtests.minute --strategy SCRIPT --input AD_JSON --output DIR"""

import argparse
import json

from ashare_quant.backtests.minute.inputs import MinuteInputs
from ashare_quant.backtests.minute.runner import run_backtest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--strategy")
    p.add_argument("--output")
    p.add_argument("--config", help="JSON execution settings")
    p.add_argument("--accept-assumed-visibility", action="store_true")
    p.add_argument("--accept-state-replay", action="store_true")
    p.add_argument(
        "--accept-research",
        action="store_true",
        help="accept the separately named bounded TDX research profile",
    )
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--timeout", type=float, default=120)
    a = p.parse_args()
    if a.preflight:
        result = MinuteInputs(
            a.input,
            accept_assumed=a.accept_assumed_visibility,
            accept_state_replay=a.accept_state_replay,
            accept_research=a.accept_research,
        ).preflight()
    else:
        if not a.strategy or not a.output:
            p.error("--strategy and --output are required for execution")
        result = run_backtest(
            a.strategy,
            a.input,
            a.output,
            config=json.loads(a.config) if a.config else None,
            accept_assumed=a.accept_assumed_visibility,
            accept_state_replay=a.accept_state_replay,
            accept_research=a.accept_research,
            timeout=a.timeout,
        )
        result = {
            "native_run": result["native_run"],
            "identity": result["result"]["identity"],
            "trades": len(result["result"]["trades"]),
            "output": a.output,
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
