"""Create a fresh local review store from a fixed AD consumer package."""

import argparse
import json
from pathlib import Path

from ashare_quant.paper.execution import bind, ingest
from ashare_quant.paper.replay import advance
from ashare_quant.paper.service import PaperService
from ashare_quant.storage.artifacts import atomic

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = args.execution_source.resolve()
    spec = json.loads((source / "consumer.json").read_text())
    risk = json.loads((REPO / "examples/daily-paper-research-risk.json").read_text())
    service = PaperService(output / "store")
    cases = {}
    for name, window, policy, strategy in (
        ("dividend-review", "w2022", risk, "equal-weight"),
        ("missing-state-review", "w2024", risk, "equal-weight"),
        ("conservative-review", "w2020", {}, "equal-weight"),
        ("observation-review", "w2024", risk, "trend"),
    ):
        item = bind(
            service,
            REPO / "examples/portfolio/manifest.json",
            source / "research/binding.json",
            spec["research_sha"],
            source / "execution/binding.json",
            spec["execution_sha"],
            source / spec["wheel"],
            spec["wheel_sha"],
            spec["ad_commit"],
            strategy,
            window,
            name,
            accepted_assumptions=spec["accepted_assumptions"],
            accept_listing_continuity=spec["accept_listing_continuity"],
            cash_credit_policy=spec["cash_credit_policy"],
            consumer_contract=source / spec["consumer_contract"]
            if spec.get("consumer_contract")
            else None,
            consumer_contract_sha=spec.get("consumer_contract_sha"),
            risk=policy,
        )
        sid, parent = item["session_id"], None
        if name == "dividend-review":
            for day in service.view(sid)["replay"]["days"]:
                if day > "2022-04-07":
                    break
                ingest(service, sid, day, parent)
                report = advance(service, sid, day, parent)
                parent = report["checkpoint_sha256"]
                print(name, day, parent, flush=True)
        cases[name] = {**item, "head": parent}
    atomic(
        output / "ready.json",
        {
            "store": str(service.root),
            "cases": cases,
            "continuous_paper_accepted": False,
            "human_ui_accepted": False,
        },
    )
    print(output / "ready.json", flush=True)


if __name__ == "__main__":
    main()
