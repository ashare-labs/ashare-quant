"""Fresh static-source gate before binding; never intraday market permission."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ashare_data import phase_observation as phase
from ashare_data.daily_observation import atomic, sha
from ashare_data.storage import immutable_write


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--day", required=True)
    p.add_argument("--previous-day", required=True)
    args = p.parse_args()
    root = args.root
    root.mkdir(parents=True, exist_ok=False)
    (root / "blobs").mkdir()

    def clock():
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    start = clock()
    result = dict(
        schema="static-owner-preflight.v1",
        day=args.day,
        started_at=start.isoformat(),
        status="started",
        sources={},
        actual_market_permission=False,
    )
    bodies = {}
    try:
        if start.date().isoformat() != args.day:
            raise RuntimeError("CURRENT_DAY_STATIC_CHECK_REQUIRED")
        specs = phase.specs("admission", args.day, phase.CONDITIONAL_SCHEMA)
        for name, (url, referer) in specs.items():
            if (clock() - start).total_seconds() > 225:
                raise TimeoutError("STATIC_SOURCE_BUDGET_EXHAUSTED")
            item = dict(url=url, referer=referer, started_at=clock().isoformat())
            try:
                raw = phase.fetch(url, referer)
            except Exception as exc:
                item["error"] = dict(type=type(exc).__name__, message=str(exc))
            else:
                h = sha(raw)
                immutable_write(root / "blobs" / h, raw)
                bodies[h] = raw
                item.update(sha256=h, http_status=200)
            item["observed_at"] = clock().isoformat()
            result["sources"][name] = item
            atomic(root / "result.json", result)
        assessment = phase.assess(
            "admission", args.day, result["sources"], bodies, phase.CONDITIONAL_SCHEMA
        )
        result["assessment"] = assessment
        # Old-date night/pre-open snapshots are expected; they do not grant
        # current session limits or execution permission. Every other cause blocks.
        allowed = {"CURRENT_LIMITS_UNVERIFIED:官方快照不是当日或晚于收讫"}
        blockers = []
        for row in assessment["rows"]:
            source = result["sources"].get("sse_quote_" + row["security"][:6], {})
            body = json.loads(bodies[source["sha256"]]) if "sha256" in source else {}
            for reason in row["blockers"]:
                if reason not in allowed or body.get("date") != int(
                    args.previous_day.replace("-", "")
                ):
                    blockers.append(reason)
        if any("error" in s for s in result["sources"].values()):
            blockers.append("SOURCE_TRANSPORT_FAILED")
        if len(assessment["rows"]) != 3 or not all(
            r.get("action_scope", {}).get("price_share_code_review") for r in assessment["rows"]
        ):
            blockers.append("ACTION_REVIEW_NOT_PROVEN")
        result["static_blockers"] = sorted(set(blockers))
        result["status"] = "blocked" if blockers else "static_sources_verified"
    except Exception as exc:
        result.update(status="blocked", error=dict(type=type(exc).__name__, message=str(exc)))
    result["completed_at"] = clock().isoformat()
    atomic(root / "result.json", result)
    print(
        json.dumps(
            dict(
                status=result["status"],
                completed_at=result["completed_at"],
                sources=len(result["sources"]),
                static_blockers=result.get("static_blockers"),
                error=result.get("error"),
                evidence=str(root / "result.json"),
                actual_market_permission=False,
            ),
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "static_sources_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
