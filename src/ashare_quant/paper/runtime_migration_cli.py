"""Explicit pre-start runtime authorization; default operation only checks."""

import argparse
import json
from pathlib import Path

from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.risk import require
from ashare_quant.paper.runtime_migration import migrate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    for name in ("state-sha", "from-sha", "to-sha", "binding-sha"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--review-sha")
    p.add_argument("--reason")
    args = p.parse_args()
    try:
        require((args.root / "state.json").is_file(), "PAPER_EXISTING_ACCOUNT_REQUIRED")
        result = migrate(
            OnlineService(args.root),
            state_sha=args.state_sha,
            from_sha=args.from_sha,
            to_sha=args.to_sha,
            binding_sha=args.binding_sha,
            apply=args.apply,
            review_sha=args.review_sha,
            reason=args.reason,
        )
        print(json.dumps(dict(status="ok", result=result), indent=2))
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    status="blocked",
                    error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
                ),
                indent=2,
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
