"""Prospective candidates from current AD observations; execution stays closed.

This prerequisite is deliberately outside PaperService's historical replay API.
It neither creates an account nor posts an order, fill, or synthetic cash balance.
"""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ashare_quant.daily_portfolio import signals
from ashare_quant.m2_jobs.files import atomic, canonical, digest, lock, regular, sha

from .risk import require


def now():
    return datetime.now(timezone.utc)


def read_ad(python, source, root, capture_id):
    """Use AD's current source in an isolated process; don't upgrade native AD."""
    source = Path(source).resolve()
    inventory = {
        str(p.relative_to(source)): sha(regular(p))
        for p in sorted((source / "ashare_data").rglob("*.py"))
    }
    require(inventory, "PAPER_OBSERVATION_AD_MISSING")
    boot = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from ashare_data.daily_observation import read;"
        "print(json.dumps(read(sys.argv[2],sys.argv[3]),ensure_ascii=False))"
    )
    run = subprocess.run(
        [str(python), "-I", "-B", "-c", boot, str(source), str(Path(root).resolve()), capture_id],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    require(run.returncode == 0, "PAPER_OBSERVATION_AD_REJECTED", run.stderr[-3000:])
    require(
        inventory
        == {
            str(p.relative_to(source)): sha(regular(p))
            for p in sorted((source / "ashare_data").rglob("*.py"))
        },
        "PAPER_OBSERVATION_AD_CHANGED",
    )
    return json.loads(run.stdout), digest(inventory)


def _timestamp(text):
    value = datetime.fromisoformat(text)
    require(value.tzinfo is not None and value.utcoffset() is not None, "PAPER_OBSERVATION_CLOCK")
    return value


def _candidate(root, path):
    pointer = json.loads(regular(path))
    expected = pointer["receipt_sha256"]
    require(
        len(expected) == 64 and set(expected) <= set("0123456789abcdef"),
        "PAPER_OBSERVATION_CORRUPT",
    )
    raw = regular(root / "receipts" / (expected + ".json"))
    require(sha(raw) == expected, "PAPER_OBSERVATION_CORRUPT")
    result = json.loads(raw)
    require(path.stem == result["trading_day"], "PAPER_OBSERVATION_CORRUPT")
    return result


def freeze(root, packet, capture_id, ad_source_sha256, *, initial_cash="1000000.00"):
    """Freeze a future candidate. Caller must obtain packet using AD read_ad.

    The supplied cash is planned initial capital, not a posted account balance.
    This API cannot submit the candidate to the native worker.
    """
    root = Path(root)
    current = now()
    require(
        packet["schema"] == "ad-current-daily-observation.v1"
        and packet["evidence_kind"] == "live_local_capture"
        and packet["execution_permission"] is False
        and digest(packet) == capture_id,
        "PAPER_OBSERVATION_IDENTITY",
    )
    require(packet["signal_ready"] and not packet["revisions"], "PAPER_OBSERVATION_REVISION")
    received = _timestamp(packet["completed_at"])
    require(received <= current, "PAPER_OBSERVATION_FUTURE")
    trading_day = packet["calendar"]["next_open"]
    require(packet["signal_day"] < trading_day, "PAPER_OBSERVATION_CALENDAR")
    logical = _timestamp(trading_day + "T09:00:00+08:00")
    deadline = _timestamp(trading_day + "T09:05:00+08:00")
    require(received <= logical, "PAPER_OBSERVATION_LATE_INPUT")
    universe = tuple(sorted(packet["securities"]))
    # The frozen candidate scope must also be available from an installed wheel.
    manifest = json.loads(regular(Path(__file__).with_name("observation-universe.json")))
    require(
        len(universe) == 3 and set(universe) <= set(manifest["securities"]),
        "PAPER_OBSERVATION_UNIVERSE",
    )
    cash = Decimal(initial_cash)
    require(cash.is_finite() and cash > 0, "PAPER_OBSERVATION_CAPITAL")
    history = {s: [r["close"] for r in packet["securities"][s]["rows"]] for s in universe}
    data = {
        "signal_day": packet["signal_day"],
        "next_open": trading_day,
        "securities": {s: packet["securities"][s]["rows"] for s in universe},
        "calendar": packet["calendar"]["open_days"],
    }
    spec = {
        "strategy_id": "equal-weight",
        "strategy_sha256": sha(regular(Path(signals.__file__))),
        "ad_source_sha256": ad_source_sha256,
        "universe": universe,
        "planned_initial_cash": str(cash),
        "purpose": "prospective_candidate_only",
    }
    # JSON round-trip fixes tuple/list representation for restart comparison.
    spec = json.loads(canonical(spec))
    identity = digest(spec)
    result = {
        "schema": "paper-observation-candidate.v1",
        "plan_identity": identity,
        "spec": spec,
        "trading_day": trading_day,
        "signal_day": packet["signal_day"],
        "source_capture_id": capture_id,
        "source_received_at": packet["completed_at"],
        "data_sha256": digest(data),
        "created_at": current.isoformat(),
        "decision_at": logical.isoformat(),
        "decision_deadline": deadline.isoformat(),
        "earliest_simulated_match": trading_day + "T09:30:00+08:00",
        "signal": signals.decide("equal-weight", history, cash),
        "execution_admitted": False,
        "candidate_is_order": False,
        "account": {
            "status": "not_started",
            "native_session_id": None,
            "head": None,
            "cash": None,
            "positions": None,
            "nav": None,
            "orders": [],
            "fills": [],
        },
        "blockers": [
            "PAPER_OBSERVED_ENGINE_UNAVAILABLE",
            "PAPER_CURRENT_STATE_UNKNOWN",
            "PAPER_CURRENT_LIMITS_UNKNOWN",
            "PAPER_ACTION_COVERAGE_UNSUPPORTED",
        ],
        "continuous_paper_accepted": False,
    }
    with lock(root / "observation.lock"):
        plan = root / "plan.json"
        if plan.exists():
            require(json.loads(regular(plan)) == spec, "PAPER_OBSERVATION_PLAN_CHANGED")
        else:
            atomic(plan, spec)
        path = root / "candidates" / (trading_day + ".json")
        if path.exists():
            previous = _candidate(root, path)
            require(
                previous["plan_identity"] == identity
                and previous["data_sha256"] == digest(data)
                and previous["signal"] == result["signal"],
                "PAPER_OBSERVATION_IDEMPOTENCY_CONFLICT",
            )
            return previous
        require(current <= deadline, "PAPER_OBSERVATION_MISSED_WINDOW")
        path.parent.mkdir(exist_ok=True)
        for candidate in sorted(path.parent.glob("*.json")):
            prior = _candidate(root, candidate)
            require(prior["signal_day"] < packet["signal_day"], "PAPER_OBSERVATION_SEQUENCE")
        receipts = root / "receipts"
        receipts.mkdir(exist_ok=True)
        receipt_sha = digest(result)
        atomic(receipts / (receipt_sha + ".json"), result)
        atomic(path, {"receipt_sha256": receipt_sha})
        return result


def status(root):
    root = Path(root)
    current = now()
    candidates = []
    spec = json.loads(regular(root / "plan.json")) if (root / "plan.json").exists() else None
    for path in sorted((root / "candidates").glob("*.json")):
        item = _candidate(root, path)
        require(
            item["plan_identity"] == digest(spec) and item["spec"] == spec,
            "PAPER_OBSERVATION_PLAN_CHANGED",
        )
        require(
            item["execution_admitted"] is False
            and item["candidate_is_order"] is False
            and item["account"]["status"] == "not_started",
            "PAPER_OBSERVATION_IDENTITY",
        )
        candidates.append(
            {
                **item,
                "current_phase": "missed_window"
                if current > _timestamp(item["decision_deadline"])
                else "waiting_for_preopen_evidence",
            }
        )
    return {
        "as_of": current.isoformat(),
        "candidates": candidates,
        "status": "blocked" if candidates else "awaiting_observation",
        "native_accounts_created": 0,
        "native_orders_submitted": 0,
        "continuous_paper_accepted": False,
        "next_action": "取得及时的当日状态/限价/行动覆盖并实现在线原生入口；按真实日历继续观测。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--ad-python", required=True)
    freeze_parser.add_argument("--ad-source", type=Path, required=True)
    freeze_parser.add_argument("--ad-store", type=Path, required=True)
    freeze_parser.add_argument("--capture", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "freeze":
        packet, source_sha = read_ad(args.ad_python, args.ad_source, args.ad_store, args.capture)
        result = freeze(args.root, packet, args.capture, source_sha)
    else:
        result = status(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # A candidate is not a successful online account day.
    raise SystemExit(2)


if __name__ == "__main__":
    main()
