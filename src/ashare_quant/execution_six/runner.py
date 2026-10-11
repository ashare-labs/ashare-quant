"""One native account per fresh process. Completed outputs reopen without execution."""

import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path

from ashare_quant.identity import source_inventory
from ashare_quant.m2.common import digest
from ashare_quant.runtime import runtime_fingerprint

from .contracts import CASES, event_plan

ARTIFACTS = ("manifest.json", "strategy.py", "config.json", "native-config.json",
             "events.jsonl", "native.log", "orders.json", "trades.json",
             "account.json", "result.json", "teardown.json")


def artifact_hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ARTIFACTS}


def atomic(path, value):
    tmp = path.with_suffix(path.suffix + ".pending")
    with tmp.open("w") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def run_case(case_id, provider, root, *, verification_only=False, reservation_model=None,
             b12_control=None):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("SIX_FRESH_ISOLATED_PROCESS_REQUIRED")
    if case_id not in CASES:
        raise ValueError("SIX_UNKNOWN_CASE")
    from .opening import CONTROLS, MODEL, OpeningHost, control_plan
    if b12_control is not None and (case_id != "B12" or b12_control not in CONTROLS):
        raise ValueError("B12_CONTROL_SCOPE_REQUIRED")
    if not verification_only:
        from .owner import PublicOwner
        if type(provider) is not PublicOwner:
            raise RuntimeError("SIX_FIXED_PUBLIC_OWNER_REQUIRED")
    from rqprobe.guards import install_network_guard
    install_network_guard()
    from rqprobe.runtime import runtime_identity
    native_pin = runtime_identity()
    runtime_pin = runtime_fingerprint(native=True)
    case = CASES[case_id]
    if reservation_model is not None:
        from ashare_quant.round_one.reservation_account import MODEL
        if reservation_model != MODEL or case.style != "limit":
            raise RuntimeError("SIX_RESERVATION_MODEL_SCOPE_REQUIRED")
    descriptor = provider.descriptor()
    if verification_only and (descriptor.get("evidence_kind") != "synthetic" or descriptor.get("real_market_data") is not False):
        raise RuntimeError("SIX_SYNTHETIC_LABEL_REQUIRED")
    metadata = provider.bootstrap(case)
    if metadata["security"] != case.security or metadata["day"] != str(case.day):
        raise RuntimeError("SIX_OWNER_BINDING_MISMATCH")
    calendar = metadata["calendar"]
    if (calendar != sorted(set(calendar)) or str(case.day) not in calendar
            or (not verification_only and calendar != [str(case.day)])):
        raise RuntimeError("SIX_BOUNDED_CALENDAR_REQUIRED")
    plan = control_plan(case) if b12_control is not None else event_plan(case)
    package = Path(__file__).parent
    strategy_name = "b12_control.py" if b12_control is not None else case.strategy_path_name
    strategy_source = (package / "strategies" / strategy_name).read_text()
    frozen = {"schema": "execution-six.native.v1", "case": case.to_dict(), "provider": descriptor,
              "bootstrap": metadata, "plan": plan, "rqalpha": native_pin, "runtime": runtime_pin,
              "backend_sources": source_inventory(), "strategy_source": strategy_source,
              "verification_only": verification_only,
              "model": {"frequency": "1m", "costs": "zero", "slippage": "zero",
                        "clock_scope": "selected_observed_clocks_not_full_session",
                        "capacity": "retrospective_daily" if case_id in {"B10", "B13"} else "observed_event_volume",
                        "opening": "quote_after_0930_callback_not_independent_auction"}}
    if reservation_model is not None:
        frozen["model"]["reservation"] = reservation_model
    if case_id == "B12":
        frozen["model"].update(order_query=MODEL, b12_control=b12_control)
    identity = digest(frozen)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result_path = root / "result.json"
        complete_path = root / "complete.json"
        if complete_path.exists():
            complete = json.loads(complete_path.read_text())
            if complete["identity"] != identity or hashlib.sha256(result_path.read_bytes()).hexdigest() != complete["result_sha256"]:
                raise RuntimeError("SIX_COMPLETED_IDENTITY_MISMATCH")
            if complete.get("artifacts") != artifact_hashes(root):
                raise RuntimeError("SIX_COMPLETED_ARTIFACT_MISMATCH")
            return {"native_run": False, "reentry": True, "result": json.loads(result_path.read_text())}
        if (root / "manifest.json").exists():
            raise RuntimeError("SIX_INCOMPLETE_ATTEMPT_RETAINED_USE_NEW_DIRECTORY")
        atomic(root / "manifest.json", {"identity": identity, **frozen})
        (root / "strategy.py").write_text(strategy_source)
        atomic(root / "config.json", case.to_dict())
        from .host import Host
        from .state import runtime
        if runtime:
            raise RuntimeError("SIX_ONE_NATIVE_RUN_PER_PROCESS")
        host = (OpeningHost(case, provider, metadata, plan, root, control=b12_control)
                if case_id == "B12" else Host(case, provider, metadata, plan, root))
        runtime["host"] = host
        try:
            def configure_reservation(requested):
                if reservation_model is not None:
                    requested["mod"]["six"]["reservation_model"] = reservation_model
            result = _native(host, root, configure=configure_reservation)
            if reservation_model is not None:
                result["reservation_model"] = reservation_model
                result["native_account_owner"] = type(host.env.portfolio.stock_account).__module__ + "." + type(host.env.portfolio.stock_account).__name__
            result.update(identity=identity, verification_only=verification_only,
                          evidence_kind=descriptor["evidence_kind"],
                          actual_real_case_run=not verification_only, dataset_id=descriptor["dataset_id"],
                          historical_pit=False, live_execution_permission=False)
            if verification_only:
                result["status"] = "SYNTHETIC_" + result["status"]
            else:
                result["scope"] = "conditional_sparse_native_research"
                result["conditions"], result["gaps"] = descriptor["conditions"], descriptor["gaps"]
            atomic(result_path, result)
            atomic(root / "orders.json", result["orders"])
            atomic(root / "trades.json", result["trades"])
            atomic(root / "account.json", result["final_account"])
            atomic(complete_path, {"identity": identity, "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                                   "scenario_triggered": result["scenario_triggered"], "actual_real_case_run": not verification_only,
                                   "artifacts": artifact_hashes(root)})
            return {"native_run": True, "reentry": False, "result": result}
        except Exception as exc:
            original = getattr(getattr(exc, "error", None), "exc_val", exc)
            atomic(root / "failure.json", {"identity": identity, "type": type(original).__name__,
                                          "message": str(original), "actual_real_case_run": False})
            raise
        finally:
            host.closed = True


def _native(host, root, *, mod_name="six", mod_path="ashare_quant.execution_six.host",
            configure=None, proxy_class=None):
    import logbook
    import rqalpha.main as native
    import rqalpha.utils.config as config_module
    from rqalpha.environment import Environment
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade

    from rqprobe.checkpoint import Counter

    from .native import Proxy
    source = Path(__file__).with_name("native_strategy.py").read_text()
    requested = {
        "base": {"start_date": str(host.case.day), "end_date": str(host.case.day),
                 "frequency": "1m", "accounts": {"stock": float(host.case.initial_cash)},
                 "run_type": "b", "rqdatac_uri": "disabled", "auto_update_bundle": False,
                 "data_bundle_path": str(root / "ABSENT_BUNDLE"), "persist": False,
                 "capital_gain_tax_rate": 0, "partial_fill_on_insufficient_cash": False},
        "extra": {"log_level": "warning", "locale": "en_US"},
        "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": False},
                "sys_scheduler": {"enabled": False},
                "sys_simulation": {"matching_type": "current_bar", "signal": False,
                                   "price_limit": True, "volume_limit": True,
                                   "volume_percent": float(host.case.volume_ratio),
                                   "inactive_limit": True, "slippage": 0, "management_fee": []},
                "sys_accounts": {"stock_t1": True, "validate_stock_position": True,
                                 "auto_switch_order_value": False, "financing_rate": 0,
                                 "financing_stocks_restriction_enabled": False, "dividend_reinvestment": False},
                "sys_risk": {"validate_price": True, "validate_is_trading": True, "validate_cash": True},
                "sys_transaction_cost": {"stock_min_commission": 0, "stock_commission_multiplier": 0,
                                         "tax_multiplier": 0, "pit_tax": False},
                "six": {"enabled": True, "lib": "ashare_quant.execution_six.host", "priority": 1000}}}
    requested["mod"][mod_name] = requested["mod"].pop("six")
    requested["mod"][mod_name]["lib"] = mod_path
    if configure is not None:
        configure(requested)
    config_module.user_config = lambda: {}
    config_module.project_config = lambda: {}
    config = config_module.parse_config(json.loads(json.dumps(requested)), source_code=source)
    enabled = {k for k, v in config.mod.convert_to_dict().items() if v["enabled"]}
    if enabled != {"sys_accounts", "sys_risk", "sys_simulation", "sys_transaction_cost", mod_name}:
        raise RuntimeError("SIX_UNEXPECTED_NATIVE_PLUGIN")
    atomic(root / "native-config.json", requested)
    Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)

    def forbid_bundle(*args, **kwargs):
        raise RuntimeError("SIX_DEFAULT_BUNDLE_FORBIDDEN")
    native.BaseDataSource, native.DataProxy = forbid_bundle, proxy_class or Proxy
    try:
        with logbook.FileHandler(str(root / "native.log"), level="DEBUG").applicationbound():
            results = native.run(config, source_code=source)
        if not results or mod_name not in results:
            raise RuntimeError("SIX_NATIVE_COMPLETION_MISSING")
        return results[mod_name]
    finally:
        if Environment._env is not None:
            native.cleanup_resources(Environment._env)
        atomic(root / "teardown.json", {"native_singleton_cleared": Environment._env is None})
