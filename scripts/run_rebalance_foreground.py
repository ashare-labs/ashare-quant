"""One bounded isolated rebalance action. No scheduler, daemon or broker."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--package-sha", required=True)
    parser.add_argument(
        "action",
        choices=[
            "preflight",
            "premarket",
            "audit",
            "admission",
            "opening",
            "closing",
            "status",
            "resume",
        ],
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Only preflight: three read-only connectivity probes",
    )
    parser.add_argument(
        "--capture",
        help="Explicit previously collected D-day daily packet for delayed closing",
    )
    parser.add_argument("--reason", help="Required for explicit failed/running recovery")
    args = parser.parse_args()
    raw = args.package.read_bytes()
    if digest(raw) != args.package_sha:
        raise RuntimeError("PACKAGE_CHANGED")
    p = json.loads(raw)
    for name, h in p["frozen_files"].items():
        path = Path(name)
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != h:
            raise RuntimeError("FROZEN_FILE_CHANGED: " + name)
    root = Path(p["run_directory"])
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "operator.lock").open("a+b")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    label = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-"
        + args.action
        + "-"
        + uuid4().hex[:8]
    )
    log = root / label
    log.mkdir()
    result = dict(
        action=args.action,
        started_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        package_sha256=args.package_sha,
        log=str(log),
        steps=[],
        status="started",
    )
    env = dict(os.environ, **p["common_env"], PYTHONDONTWRITEBYTECODE="1")
    day = p["day"]
    plan = p["plan"]
    account = Path(p["account_root"])
    state_path = account / "state.json"

    def save():
        (log / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    def run(name, argv, owner="aq", network=False):
        index = len(result["steps"])
        stem = f"{index:02d}-{name}"
        child_env = dict(env, PYTHONPATH=p[owner + "_source"])
        if network:
            child_env.update(p["network_env"])
        item = dict(name=name, argv=argv, started_at=datetime.now(timezone.utc).isoformat())
        result["steps"].append(item)
        save()
        with (
            (log / (stem + ".stdout")).open("w") as out,
            (log / (stem + ".stderr")).open("w") as err,
        ):
            try:
                done = subprocess.run(
                    argv,
                    env=child_env,
                    stdout=out,
                    stderr=err,
                    timeout=300,
                    check=False,
                )
                item["returncode"] = done.returncode
            finally:
                item["finished_at"] = datetime.now(timezone.utc).isoformat()
                save()
        text = (log / (stem + ".stdout")).read_text()
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        item["output_file"] = str(log / (stem + ".stdout"))
        save()
        if done.returncode != 0:
            raise RuntimeError(name + " rejected; inspect " + str(log / (stem + ".stdout")))
        return value

    def aq(command, *extra):
        return run(command, p["intraday_argv"] + [command, *extra])["result"]

    def state():
        return json.loads(state_path.read_bytes())

    def verified():
        s = state()
        if (
            s["identity"] != p["account_identity"]
            or s["config"]["scenario_manifest_sha256"] != p["plan_sha256"]
        ):
            raise RuntimeError("ISOLATED_ACCOUNT_IDENTITY_CHANGED")
        return s, aq("status")

    def window(end):
        at = datetime.now(timezone.utc)
        if not datetime.fromisoformat(plan["start_at"]) <= at <= datetime.fromisoformat(plan[end]):
            raise RuntimeError("ACTUAL_WINDOW_CLOSED; no backfill or automatic deadline extension")

    try:
        save()
        s = state()
        if args.action in ("preflight", "status"):
            _, value = verified()
            result["account_status"] = value
            result["initial_inventory"] = plan["initial_inventory"]
            result["target_quantities"] = plan["target_quantities"]
            if args.network:
                if args.action != "preflight":
                    raise RuntimeError("NETWORK_PROBE_REQUIRES_PREFLIGHT")
                script = log / "network_probe.py"
                shutil.copy2(p["network_probe"], script)
                result["network"] = run(
                    "network-probe", [p["ad_python"], "-B", str(script)], "ad", True
                )
                if any("error" in r or r.get("http_status") != 200 for r in result["network"]):
                    raise RuntimeError("COLLECTION_TRANSPORT_UNAVAILABLE")
            at = datetime.now(timezone.utc)
            start = datetime.fromisoformat(plan["start_at"])
            deadline = datetime.fromisoformat(plan["intent_deadline"])
            execution_end = datetime.fromisoformat(plan["execution_deadline"])
            result["scheduled_window_state"] = (
                "upcoming"
                if at < start
                else "intent_open"
                if at <= deadline
                else "execution_only"
                if at <= execution_end
                else "expired"
            )
            result["ready_for_scheduled_admission"] = at <= deadline
            result["market_permission"] = (
                "requires actual D-day fresh admission and post-intent completed minute"
            )
        elif args.action == "premarket":
            verified()
            result["fresh_static_sources"] = run(
                "static-owner-preflight",
                [
                    p["ad_python"],
                    "-B",
                    p["owner_preflight"],
                    "--root",
                    str(log / "owner-preflight"),
                    "--day",
                    day,
                    "--previous-day",
                    p["previous_day"],
                ],
                "ad",
                True,
            )
        elif args.action == "audit":
            s, _ = verified()
            row = s["days"].get(day)
            if not row or "opening" not in row["reports"]:
                raise RuntimeError("NO_EXECUTION_REPORT_YET")
            phase = "closing" if "closing" in row["reports"] else "opening"
            report = aq("advance", "--phase", phase)
            from decimal import Decimal as D

            n = report["native"]
            trades = n["trades"]
            positions = n["account"]["positions"]
            _, cfg = s["identity"], s["config"]
            expected = D(n["before"]["total_cash"])
            for t in trades:
                value = D(t["price"]) * t["quantity"]
                fee = max(D(cfg["minimum_commission"]), value * D(cfg["commission_rate"]))
                tax = value * D(cfg["sell_tax_rate"]) if t["side"] == "SELL" else D(0)
                if abs(D(t["commission"]) - fee) > D("1e-8") or abs(D(t["tax"]) - tax) > D("1e-8"):
                    raise RuntimeError("NATIVE_FEE_RECONCILIATION_FAILED")
                expected += (value if t["side"] == "SELL" else -value) - D(t["cost"])
            if abs(expected - D(n["account"]["total_cash"])) > D("1e-8"):
                raise RuntimeError("NATIVE_CASH_RECONCILIATION_FAILED")
            if len({t["id"] for t in trades}) != len(trades):
                raise RuntimeError("DUPLICATE_NATIVE_TRADE")
            if any(
                v["sellable"] != min(plan["initial_inventory"]["quantities"][sid], v["quantity"])
                for sid, v in positions.items()
            ):
                raise RuntimeError("SAME_DAY_T1_PROOF_FAILED")
            result["acceptance"] = dict(
                actual_trading_day=day,
                phase=phase,
                real_observation=report["real_calendar_observation"],
                declared_initial_inventory=plan["initial_inventory"],
                prior_trades_claimed=False,
                sells=[t for t in trades if t["side"] == "SELL"],
                buys=[t for t in trades if t["side"] == "BUY"],
                trade_count=len(trades),
                fees=str(sum(D(t["cost"]) for t in trades)),
                account=n["account"],
                cash_reconciled=True,
                t1_reconciled=True,
                target_gaps=n["target_gaps"],
                refusals=n["refusals"],
                checkpoint_sha256=report["checkpoint_sha256"],
                all_planned_cases_observed=not n["target_gaps"] and len(trades) == 3,
                day_closed=phase == "closing",
            )
        elif args.action == "admission":
            s, _ = verified()
            row = s["days"].get(day)
            if row:
                capture = row["decision"]["strategy_receipt"]["admission_capture"]
            else:
                window("intent_deadline")
                attempt = run(
                    "admission-capture",
                    p["phase_argv"]
                    + [
                        "capture",
                        "--kind",
                        "admission",
                        "--day",
                        day,
                        "--not-before",
                        plan["start_at"],
                    ],
                    "ad",
                    True,
                )
                capture = attempt["capture_id"]
            result["intent"] = aq("prepare", "--capture", capture)
        elif args.action == "opening":
            s, _ = verified()
            row = s["days"].get(day)
            if not row:
                raise RuntimeError("ACTUAL_INTENT_REQUIRED")
            if "opening" in row["reports"]:
                result["opening"] = aq("advance", "--phase", "opening")
            else:
                if row["status"] in ("failed", "running"):
                    raise RuntimeError("EXPLICIT_RESUME_REQUIRED; preserve sealed inputs")
                if row["opening"] is None:
                    window("execution_deadline")
                    intent = datetime.fromisoformat(row["decision"]["received_at"])
                    earliest = intent.replace(second=0, microsecond=0) + timedelta(minutes=3)
                    if datetime.now(timezone.utc) < earliest:
                        raise RuntimeError("WAIT_COMPLETE_MINUTE_UNTIL " + earliest.isoformat())
                    attempt = run(
                        "execution-capture",
                        p["phase_argv"]
                        + [
                            "capture",
                            "--kind",
                            "execution",
                            "--day",
                            day,
                            "--not-before",
                            row["decision"]["received_at"],
                        ],
                        "ad",
                        True,
                    )
                    aq("deliver", "--capture", attempt["capture_id"])
                # AQ rechecks the real deadline before spawning. A complete sealed
                # checkpoint may still be published after restart without new execution.
                result["opening"] = aq("advance", "--phase", "opening")
        elif args.action == "closing":
            s, _ = verified()
            row = s["days"].get(day)
            if not row or "opening" not in row["reports"]:
                raise RuntimeError("PUBLISHED_ACTUAL_DAY_OPENING_REQUIRED")
            if "closing" in row["reports"]:
                result["closing"] = aq("advance", "--phase", "closing")
            else:
                if row["status"] in ("failed", "running"):
                    raise RuntimeError("EXPLICIT_RESUME_REQUIRED; preserve sealed inputs")
                capture = row["close"]["data_manifest_sha256"] if row["close"] else args.capture
                if capture is None:
                    at = datetime.now(ZoneInfo("Asia/Shanghai"))
                    if at.date().isoformat() != day or (at.hour, at.minute) < (15, 0):
                        raise RuntimeError("WAIT_FOR_D_DAY_CONFIRMED_CLOSE; no later-day backfill")
                    attempt = run("daily-capture", p["daily_argv"], "ad", True)
                    capture = attempt["capture_id"]
                if args.capture and row["close"] and args.capture != capture:
                    raise RuntimeError("SEALED_CLOSE_CAPTURE_CONFLICT")
                aq("close", "--capture", capture)
                result["closing"] = aq("advance", "--phase", "closing")
        elif args.action == "resume":
            s, _ = verified()
            row = s["days"].get(day)
            if not row or row["status"] not in ("failed", "running"):
                result["recovery"] = "no_failed_or_running_phase"
            else:
                if not args.reason or not args.reason.strip():
                    raise RuntimeError("RECOVERY_REASON_REQUIRED")
                # A native child can retain this same global lock after controller death.
                with Path(p["account_global_lock"]).open("a+b") as global_stream:
                    fcntl.flock(global_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result["recovery"] = aq("resume", "--reason", args.reason)
        result.update(status="ok", finished_at=datetime.now(timezone.utc).isoformat())
        save()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        result.update(
            status="blocked",
            error=dict(type=type(exc).__name__, message=str(exc)),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        save()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
        )
        sys.exit(2)
