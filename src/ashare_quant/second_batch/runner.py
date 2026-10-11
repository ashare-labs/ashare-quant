"""Bounded B40-B45 original-source runner; a fresh isolated process per account."""

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

from rqprobe.guards import install_network_guard
from rqprobe.runtime import runtime_identity

from ashare_quant.execution_six.runner import _native, atomic
from ashare_quant.execution_six.state import runtime
from ashare_quant.identity import source_inventory
from ashare_quant.runtime import runtime_fingerprint

from .case import Case
from .host import Host
from .native import Proxy
from .owner import Owner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("SECOND_FRESH_ISOLATED_PROCESS_REQUIRED")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    request = json.loads(Path(args.request).read_text())
    script = Path(request["script"])
    raw = script.read_bytes()
    if hashlib.sha256(raw).hexdigest() != request["script_sha256"]:
        raise RuntimeError("SECOND_SOURCE_HASH")
    config = next(
        ast.literal_eval(n.value)
        for n in ast.parse(raw).body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in n.targets)
    )
    if request["policy"] not in {"native-before", "fixed", "integrated"}:
        raise RuntimeError("SECOND_UNKNOWN_POLICY")
    case = Case(config, str(script), request["policy"])
    if case.case_id not in {"B40", "B41", "B42", "B43", "B44", "B45"}:
        raise RuntimeError("SECOND_CASE_SCOPE")
    owner = Owner(
        request["owner_binding"],
        request["owner_binding_sha256"],
        request["owner_paths"],
        root / "owner-reads.jsonl",
        expected_sources=request["owner_sources"],
    )
    try:
        desc = owner.call("describe")
        previous = {
            r["session"]: r["previous"]
            for r in desc["calendars"]
            if config["start_date"] <= r["session"] <= config["end_date"]
        }
        sessions = sorted(previous)
        if (
            not sessions
            or sessions[0] != config["start_date"]
            or sessions[-1] != config["end_date"]
        ):
            raise RuntimeError("SECOND_CALENDAR_MISSING")
        calendar = sorted(set(sessions) | set(previous.values()))
        metadata = dict(calendar=calendar, previous=previous)
        plan = []
        for day in sessions:
            for clock, kind in [
                ("09:00", "before"),
                *[(t, "callback") for t in config["probe_times"]],
                ("15:30", "after"),
            ]:
                plan.append(
                    dict(at=day + "T" + clock + ":00+08:00", kind=kind, index=len(plan) + 1)
                )
        plan.append(
            dict(at=case.end_day + "T23:59:59+08:00", kind="end_clock", index=len(plan) + 1)
        )
        manifest = dict(
            request=request,
            config=config,
            metadata=metadata,
            plan=plan,
            backend_sources=source_inventory(),
            rqalpha=runtime_identity(),
            runtime_versions=runtime_fingerprint(native=True),
            original_script_sha256=request["script_sha256"],
            owner_descriptor=desc,
            models=dict(
                capacity=(
                    "immediate retrospective daily volume; resting observed minute volume"
                    if case.policy == "integrated"
                    else "retrospective daily volume"
                ),
                price=(
                    "immediate observed quote; resting buy limit follows documented JQ bar-limit policy"
                    if case.policy == "integrated"
                    else "observed point or explicit retrospective minute close"
                ),
                reservation=(
                    "native account order budget less actual fills and fees"
                    if case.policy == "integrated"
                    else "native proportional reservation"
                ),
                valuation_points=(
                    "explicit exact-label AD retrospective TDX points on missing marks"
                    if case.policy == "integrated"
                    else "missing marks unverified"
                ),
                actions=(
                    "issuer implementation dates; 09:00 source prevclose reconstruction is a research-clock assumption"
                    if case.policy == "integrated"
                    else "issuer ratio plus ex-date credit/tradability assumption"
                ),
                tax="existing JQ fixed 20% simulation only",
                lifecycle="active CS in finite supplied sessions; no terminal liquidation",
            ),
        )
        atomic(root / "manifest.json", manifest)
        atomic(root / "config.json", config)
        (root / "strategy.py").write_bytes(raw)
        install_network_guard()
        host = Host(case, owner, metadata, plan, root)
        runtime["host"] = host

        def configure(x):
            x["base"]["end_date"] = case.end_day
            x["mod"]["sys_accounts"]["dividend_tax_enabled"] = False
            x["mod"]["sys_transaction_cost"].update(
                stock_min_commission=config["min_commission"],
                stock_commission_multiplier=config["open_commission"] / 0.0008,
                tax_multiplier=0,
                pit_tax=False,
            )
            if config["open_commission"] != config["close_commission"] or any(
                config[k] != 0
                for k in ["open_tax", "close_tax", "close_today_commission", "fixed_slippage"]
            ):
                raise RuntimeError("SECOND_COST_SCOPE")

        result = _native(
            host,
            root,
            mod_name="second_batch",
            mod_path="ashare_quant.second_batch.host",
            configure=configure,
            proxy_class=Proxy,
        )
        atomic(root / "result.json", result)
        for k in ["orders", "trades", "final_account"]:
            atomic(root / (k + ".json"), result[k])
        atomic(
            root / "complete.json",
            dict(
                artifacts={
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.iterdir()
                    if p.is_file()
                },
                actual_real_case_run=True,
            ),
        )
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in [
                        "case_id",
                        "submissions",
                        "gaps",
                        "source_not_triggered",
                        "source_errors",
                    ]
                }
            )
        )
    except Exception as exc:
        atomic(root / "failure.json", dict(type=type(exc).__name__, message=str(exc)))
        raise
    finally:
        owner.close()


if __name__ == "__main__":
    main()
