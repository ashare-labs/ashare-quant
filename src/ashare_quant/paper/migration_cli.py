"""Shared parser and response for the two existing explicit migration commands."""

import argparse
import json
from pathlib import Path

from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.risk import require


def run(description, migrate, *, binding_required=False, ensure_ascii=True):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", type=Path, required=True)
    names = ("state-sha", "from-sha", "to-sha")
    if binding_required:
        names += ("binding-sha",)
    for name in names:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review-sha")
    parser.add_argument("--reason")
    args = parser.parse_args()
    try:
        require((args.root / "state.json").is_file(), "PAPER_EXISTING_ACCOUNT_REQUIRED")
        arguments = {name.replace("-", "_"): getattr(args, name.replace("-", "_"))
                     for name in names}
        result = migrate(OnlineService(args.root), **arguments, apply=args.apply,
                         review_sha=args.review_sha, reason=args.reason)
        print(json.dumps(dict(status="ok", result=result), ensure_ascii=ensure_ascii, indent=2))
    except Exception as exc:
        print(json.dumps(dict(status="blocked", error=dict(
            code=getattr(exc, "code", type(exc).__name__), message=str(exc),
        )), ensure_ascii=ensure_ascii, indent=2))
        raise SystemExit(2)
