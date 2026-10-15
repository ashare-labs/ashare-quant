"""Run a frozen multi-security acceptance question in a fresh native process."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from decimal import Decimal
from importlib import metadata
from pathlib import Path


def run(args):
    from rqprobe.guards import install_network_guard

    install_network_guard()
    from ashare_quant.identity import source_inventory
    from ashare_quant.rq_mod import atomic_json
    from ashare_quant.runtime import runtime_fingerprint
    from rqprobe.checkpoint import AtomicFile, Counter
    from rqprobe.runtime import runtime_identity

    from .audit import audit, write_report
    from .inputs import Inputs, digest, require

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    config = next(s for s in manifest["strategies"] if s["id"] == args.strategy)
    question_dir = args.manifest.parent / args.strategy
    require(
        json.loads((question_dir / "config.json").read_text()) == config, "QUESTION_CONFIG_DRIFT"
    )
    window = next(w for w in manifest["windows"] if w["id"] == args.window)
    source = (question_dir / "strategy.py").read_text()
    data = Inputs(
        args.input,
        manifest,
        window,
        accept_price_only_research=args.accept_price_only_research,
    )
    preflight = data.preflight()
    if preflight["state_mode"] == "retrospective_model" and not args.accept_owner_state_model:
        preflight["ready"] = False
        preflight["gaps"].append(
            {
                "field": "state_model_opt_in",
                "detail": "Explicit --accept-owner-state-model required; strict state remains unavailable",
            }
        )
    atomic_json(root / (args.invocation + ".preflight.json"), preflight)
    if args.preflight:
        print(json.dumps(preflight, ensure_ascii=False))
        return 0 if preflight["ready"] else 2
    require(preflight["ready"], "DATA_GAPS_BLOCK_EXECUTION:see_preflight.json")
    require(
        args.fault == "none"
        or re.fullmatch(
            r"(before_replace|after_replace|decision_candidate|native_submit|after_event):[1-9][0-9]*",
            args.fault,
        ),
        "INVALID_FAULT",
    )
    with (root / "writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = []
        for day in data.days:
            for event, clock in (
                ("BEFORE_TRADING", "09:00:00"),
                ("BAR", "09:30:00"),
                ("AFTER_TRADING", "15:30:00"),
            ):
                records.append(
                    {"cursor": len(records) + 1, "event": event, "dt": day + "T" + clock}
                )
        identity_details = {
            "manifest": manifest,
            "strategy": args.strategy,
            "window": args.window,
            "input_sha256": data.sha256,
            "strategy_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "source_inventory": source_inventory(),
            "records": records,
            "native_runtime": runtime_identity(),
            "runtime": runtime_fingerprint(native=True),
        }
        identity = digest(identity_details)
        checkpoint = AtomicFile(root / "native.chk", identity)
        previous = checkpoint.read()
        if previous and previous["complete"]:
            atomic_json(root / "result.json", previous["receipt"])
            report = audit(previous["receipt"], data, manifest)
            write_report(root, previous["receipt"], report)
            print(
                json.dumps(
                    {"status": "completed_reentry", "identity": identity, "native_run": False}
                )
            )
            return 0 if report["passed"] else 3
        receipt = root / (args.invocation + ".manifest.json")
        require(not receipt.exists(), "INVOCATION_EXISTS_USE_NEW_LABEL")
        atomic_json(receipt, identity_details)
        callback = runpy.run_path(str(question_dir / "strategy.py"))["on_decision"]
        from .native import Helper, Proxy, runtime

        runtime.update(
            root=root,
            data=data,
            identity=identity,
            manifest=manifest,
            strategy=args.strategy,
            records=records,
            callback=callback,
        )
        import rqalpha.main as native
        import rqalpha.utils.config as config_module
        from rqalpha.model.order import Order
        from rqalpha.model.trade import Trade

        execution = manifest["execution"]
        volume_ratio = execution.get("volume_ratio")
        if volume_ratio is not None:
            volume_ratio = Decimal(str(volume_ratio))
            require(
                volume_ratio.is_finite() and 0 < volume_ratio <= 1,
                "INVALID_VOLUME_RATIO",
            )
        require(
            execution["fees"]
            == {
                "buy_commission_rate": "0.0003",
                "sell_commission_rate": "0.0003",
                "minimum_commission": "5.00",
                "sell_tax_rate": "0.001",
                "transfer_fee": "0",
                "slippage": "0",
                "scope": "fixed acceptance cost model for all windows, not a statement of historical statutory rates",
            },
            "UNSUPPORTED_COST_MODEL",
        )
        requested = {
            "base": {
                "start_date": window["start"],
                "end_date": window["end"],
                "frequency": "1d",
                "accounts": {"stock": float(execution["initial_cash"])},
                "run_type": "b",
                "rqdatac_uri": "disabled",
                "auto_update_bundle": False,
                "data_bundle_path": str(root / "ABSENT_BUNDLE"),
                "persist": True,
                "persist_mode": "real_time",
                "capital_gain_tax_rate": 0,
                "partial_fill_on_insufficient_cash": False,
            },
            "extra": {"log_level": "error", "locale": "en_US"},
            "mod": {
                "sys_progress": {"enabled": False},
                "sys_analyser": {"enabled": False},
                "sys_simulation": {
                    "matching_type": "current_bar",
                    "signal": False,
                    "price_limit": True,
                    "volume_limit": volume_ratio is not None,
                    **({"volume_percent": float(volume_ratio)} if volume_ratio is not None else {}),
                    "inactive_limit": False,
                    "slippage": 0,
                    "management_fee": [],
                },
                "sys_accounts": {
                    "stock_t1": True,
                    "validate_stock_position": True,
                    "auto_switch_order_value": False,
                    "financing_rate": 0,
                    "financing_stocks_restriction_enabled": False,
                    "dividend_reinvestment": False,
                    "dividend_tax_enabled": False,
                    "dividend_tax_rate": 0,
                },
                "sys_risk": {
                    "validate_price": True,
                    "validate_is_trading": True,
                    "validate_cash": True,
                },
                "sys_transaction_cost": {
                    "stock_min_commission": 5,
                    "stock_commission_multiplier": 0.375,
                    "tax_multiplier": 2,
                    "pit_tax": False,
                },
                "dailyportfolio": {
                    "enabled": True,
                    "lib": "ashare_quant.daily_portfolio.native",
                    "priority": 1000,
                },
            },
        }
        wrapper = Path(__file__).with_name("strategy.py").read_text()
        config_module.user_config = lambda: {}
        config_module.project_config = lambda: {}
        parsed = config_module.parse_config(requested, source_code=wrapper)
        enabled = {k for k, v in parsed.mod.convert_to_dict().items() if v["enabled"]}
        require(
            enabled
            == {
                "sys_accounts",
                "sys_risk",
                "sys_simulation",
                "sys_transaction_cost",
                "sys_scheduler",
                "dailyportfolio",
            },
            "UNEXPECTED_NATIVE_PLUGIN",
        )
        Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)

        def forbidden(*a, **k):
            raise RuntimeError("DEFAULT_DATA_BUNDLE_DISABLED")

        native.BaseDataSource, native.PersistHelper, native.DataProxy = forbidden, Helper, Proxy
        if args.fault != "none":
            os.environ["PROBE_KILL"] = args.fault
        else:
            os.environ.pop("PROBE_KILL", None)
        try:
            native.run(parsed, source_code=wrapper)
            final = checkpoint.read()
            require(final and final["complete"], "NATIVE_DID_NOT_COMPLETE")
            atomic_json(root / "result.json", final["receipt"])
            report = audit(final["receipt"], data, manifest)
            write_report(root, final["receipt"], report)
            require(report["accounting_passed"], "ACCOUNTING_AUDIT_FAILED:see_audit.json")
            print(
                json.dumps(
                    {
                        "status": "completed"
                        if report["passed"]
                        else "completed_with_contract_difference",
                        "acceptance_passed": report["passed"],
                        "identity": identity,
                        "native_run": True,
                        "trades": len(final["receipt"]["trades"]),
                    }
                )
            )
            return 0 if report["passed"] else 3
        except Exception as exc:
            error = getattr(getattr(exc, "error", None), "exc_val", exc)
            atomic_json(
                root / (args.invocation + ".failure.json"),
                {"type": type(error).__name__, "message": str(error)},
            )
            raise
        finally:
            if runtime.get("mod"):
                native.cleanup_resources(runtime["mod"].env)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument(
        "--strategy",
        required=True,
        choices=[
            "equal-weight",
            "momentum",
            "trend",
            "weekly-reversal",
            "daily-equal-weight",
            "low-volatility",
            "momentum-low-volatility",
        ],
    )
    p.add_argument("--window", required=True)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--invocation", default="first")
    p.add_argument("--fault", default="none")
    p.add_argument("--preflight", action="store_true")
    p.add_argument(
        "--accept-price-only-research",
        action="store_true",
        help="Run an explicitly labeled price-only model without corporate actions; not economic acceptance",
    )
    p.add_argument("--accept-owner-state-model", action="store_true")
    args = p.parse_args()
    if not re.fullmatch("[A-Za-z0-9_-]+", args.invocation):
        p.error("invocation must be filename-safe")
    if sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode:
        raise SystemExit(run(args))
    paths = [
        str(Path(__file__).resolve().parents[2]),
        str(metadata.distribution("rqalpha").locate_file("")),
    ]
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['daily-portfolio',*a];from ashare_quant.daily_portfolio.__main__ import main;main()"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    env.update(
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
        TZ="Asia/Shanghai",
    )
    raise SystemExit(
        subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, sys.argv[1:]])],
            env=env,
        ).returncode
    )


if __name__ == "__main__":
    main()
