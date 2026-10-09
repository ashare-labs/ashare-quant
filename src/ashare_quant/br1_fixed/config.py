"""Same fixed dates, cash, checks and matching assumptions as the original BR1."""
import json

from rqalpha.utils import config as config_module


def fixed_config(root, store, run_identity, release_sha256, source):
    events_path, checkpoint, result = root / "inputs.json", root / "native.chk", root / "receipt.json"
    requested = {
        "base": {"start_date": "2020-01-03", "end_date": "2020-01-06", "frequency": "1d",
                 "accounts": {"stock": 10000}, "run_type": "b", "rqdatac_uri": "disabled",
                 "auto_update_bundle": False, "data_bundle_path": str(root / "ABSENT_BUNDLE"),
                 "persist": True, "persist_mode": "real_time", "capital_gain_tax_rate": 0,
                 "partial_fill_on_insufficient_cash": False},
        "extra": {"log_level": "error", "locale": "en_US"},
        "mod": {
            "sys_progress": {"enabled": False}, "sys_analyser": {"enabled": False},
            "sys_simulation": {"matching_type": "current_bar", "signal": False,
                "price_limit": True, "volume_limit": True, "volume_percent": 0.01,
                "inactive_limit": True, "slippage": 0, "management_fee": []},
            "sys_accounts": {"stock_t1": True, "validate_stock_position": True,
                "auto_switch_order_value": False, "financing_rate": 0,
                "financing_stocks_restriction_enabled": False, "dividend_reinvestment": False},
            "sys_risk": {"validate_price": True, "validate_is_trading": True, "validate_cash": True},
            "sys_transaction_cost": {"stock_min_commission": 5, "stock_commission_multiplier": 1,
                                      "tax_multiplier": 1, "pit_tax": True},
            "bounded": {"enabled": True, "lib": "ashare_quant.br1_fixed.mod", "priority": 1000,
                "store": str(store), "events_path": str(events_path),
                "checkpoint": str(checkpoint), "result": str(result), "run_identity": run_identity,
                "trace": str(root / "trace.jsonl"),
                "release_sha256": release_sha256}}}
    config_module.user_config = lambda: {}
    config_module.project_config = lambda: {}
    saved = json.loads(json.dumps(requested))
    config = config_module.parse_config(json.loads(json.dumps(requested)), source_code=source)
    enabled = {k for k, v in config.mod.convert_to_dict().items() if v["enabled"]}
    if enabled != {"sys_accounts", "sys_risk", "sys_simulation", "sys_transaction_cost",
                   "sys_scheduler", "bounded"}:
        raise RuntimeError("UNAPPROVED_NATIVE_MOD_SET")
    if config.extra.enable_profiler or config.extra.context_vars:
        raise RuntimeError("EXTERNAL_EXECUTION_CONFIGURATION_FORBIDDEN")
    return config, saved
