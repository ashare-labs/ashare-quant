"""Sealed fresh-process native fee execution; no reference output import."""

import fcntl
import hashlib
import importlib
import json
import sys
from pathlib import Path

from ashare_quant.execution_six.contracts import event_plan
from ashare_quant.execution_six.runner import _native, artifact_hashes, atomic
from ashare_quant.identity import source_inventory
from ashare_quant.m2.common import digest
from ashare_quant.runtime import runtime_fingerprint

from .cases import CASES


def configure(case, requested):
    requested["base"]["end_date"] = case.end_day
    requested["mod"]["sys_transaction_cost"] = case.native_costs()
    requested["mod"]["sys_accounts"]["auto_switch_order_value"] = getattr(
        case, "auto_switch_order_value", False
    )
    requested["mod"]["sys_simulation"].update(
        slippage_model="TickSizeSlippage", slippage=float(case.fixed_spread) / 0.02
    )
    if getattr(case, "cash_model", None) is not None:
        requested["mod"]["round_one"]["cash_model"] = case.cash_model
        requested["mod"]["sys_accounts"]["dividend_tax_enabled"] = False


def run_case(case_id, provider, root, *, verification_only=False):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("ROUND_ONE_FRESH_PROCESS_REQUIRED")
    from ashare_quant.execution_six.state import runtime
    from rqprobe.guards import install_network_guard
    from rqprobe.runtime import runtime_identity

    from .owner import PublicOwner

    install_network_guard()
    case = CASES[case_id]
    calendar_only = getattr(case, "owner_profile", None) == "bounded_calendar"
    state_only = calendar_only or (
        case_id == "B35" and getattr(case, "owner_profile", None) == "empty_sessions"
    )
    dividend = getattr(case, "owner_profile", None) == "known_cash_dividend"
    carry = dividend or getattr(case, "owner_profile", None) == "bounded_carry"
    if dividend:
        from .dividend_owner import DividendOwner

        expected_provider = DividendOwner
        if verification_only:
            raise RuntimeError("B26_REAL_PUBLIC_FACT_PROVIDER_REQUIRED")
    elif carry:
        from .carry_owner import CarryOwner

        expected_provider = CarryOwner
        if verification_only:
            raise RuntimeError("ROUND_ONE_OVERNIGHT_CALENDAR_ACTION_COVERAGE_NOT_ADMITTED")
    elif calendar_only:
        from .calendar_owner import CalendarOwner

        expected_provider = CalendarOwner
        if verification_only:
            raise RuntimeError("CALENDAR_REAL_PUBLIC_PROVIDER_REQUIRED")
    elif state_only:
        from .state_owner import StateOwner

        expected_provider = StateOwner
    elif getattr(case, "owner_profile", None) == "lunch_point":
        from .point_owner import PointOwner

        expected_provider = PointOwner
    elif getattr(case, "owner_profile", None) == "priority2":
        from .multi_owner import MultiOwner

        expected_provider = MultiOwner
    else:
        expected_provider = PublicOwner
    if not verification_only and type(provider) is not expected_provider:
        raise RuntimeError("ROUND_ONE_FIXED_PUBLIC_OWNER_REQUIRED")
    descriptor = provider.descriptor()
    if verification_only and (
        descriptor.get("evidence_kind") != "synthetic"
        or descriptor.get("real_market_data") is not False
    ):
        raise RuntimeError("ROUND_ONE_SYNTHETIC_LABEL_REQUIRED")
    case = provider.case if dividend else CASES[case_id]
    if case.end_day != str(case.day) and not state_only and not carry:
        raise RuntimeError("ROUND_ONE_OVERNIGHT_CALENDAR_ACTION_COVERAGE_NOT_ADMITTED")
    metadata = provider.bootstrap(case)
    if state_only and metadata.get("engine_state_scope") != "empty_account_no_orders_or_positions":
        raise RuntimeError("STATE_ONLY_EMPTY_ACCOUNT_SCOPE_REQUIRED")
    if carry:
        if (
            metadata["security"],
            metadata["day"],
            metadata["execution_sessions"],
            metadata["research_model"],
        ) != (case.security, str(case.day), list(case.sessions), provider.model):
            raise RuntimeError("CARRY_FROZEN_SCOPE_MISMATCH")
    elif (metadata["security"], metadata["day"], metadata["calendar"]) != (
        case.security,
        str(case.day),
        metadata["calendar"]
        if calendar_only
        else [str(case.day), case.end_day]
        if state_only
        else [str(case.day)],
    ):
        raise RuntimeError("ROUND_ONE_SINGLE_SESSION_BINDING_REQUIRED")
    plan = (
        case.event_plan(metadata)
        if calendar_only
        else case.event_plan()
        if hasattr(case, "event_plan")
        else event_plan(case)
    )
    source = Path(__file__).with_name(case.strategy_path_name).read_text()
    frozen = dict(
        schema="aq.round-one.native.v1",
        case=case.to_dict(),
        provider=descriptor,
        bootstrap=metadata,
        plan=plan,
        rqalpha=runtime_identity(),
        runtime=runtime_fingerprint(native=True),
        backend_sources=source_inventory(),
        strategy_source=source,
        verification_only=verification_only,
    )
    if carry:
        frozen["original_intent"] = json.loads(
            Path(__file__)
            .with_name("dividend-intents.json" if dividend else "carry-intents.json")
            .read_text()
        )[case_id]
    if getattr(case, "query_group", None):
        frozen["query_input"] = json.loads(
            Path(__file__).with_name("query-inputs.json").read_text()
        )["cases"][case_id]
    identity = digest(frozen)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / "complete.json").exists():
            complete = json.loads((root / "complete.json").read_text())
            if complete["identity"] != identity or complete["artifacts"] != artifact_hashes(root):
                raise RuntimeError("ROUND_ONE_COMPLETED_IDENTITY_MISMATCH")
            return dict(
                native_run=False,
                reentry=True,
                result=json.loads((root / "result.json").read_text()),
            )
        if (root / "manifest.json").exists():
            raise RuntimeError("ROUND_ONE_INCOMPLETE_ATTEMPT_RETAINED_USE_NEW_DIRECTORY")
        atomic(root / "manifest.json", dict(identity=identity, **frozen))
        atomic(root / "config.json", case.to_dict())
        (root / "strategy.py").write_text(source)
        if runtime:
            raise RuntimeError("ROUND_ONE_ONE_ENGINE_PER_PROCESS")
        host_module = getattr(case, "host_module", "ashare_quant.round_one.fee_host")
        host = importlib.import_module(host_module).Host(case, provider, metadata, plan, root)
        runtime["host"] = host
        try:
            result = _native(
                host,
                root,
                mod_name="round_one",
                mod_path=host_module,
                configure=lambda value: configure(case, value),
                proxy_class=getattr(host, "proxy_class", None),
            )
            result.update(
                identity=identity,
                verification_only=verification_only,
                evidence_kind=descriptor["evidence_kind"],
                actual_real_case_run=not verification_only,
                dataset_id=descriptor["dataset_id"],
                historical_pit=False,
                live_execution_permission=False,
                conditions=descriptor.get("conditions", []),
                gaps=descriptor.get("gaps", []),
            )
            if verification_only:
                result["status"] = "SYNTHETIC_" + result["status"]
            for name, key in [
                ("result.json", None),
                ("orders.json", "orders"),
                ("trades.json", "trades"),
                ("account.json", "final_account"),
            ]:
                atomic(root / name, result if key is None else result[key])
            atomic(
                root / "complete.json",
                dict(
                    identity=identity,
                    result_sha256=hashlib.sha256((root / "result.json").read_bytes()).hexdigest(),
                    scenario_triggered=result["scenario_triggered"],
                    actual_real_case_run=not verification_only,
                    artifacts=artifact_hashes(root),
                ),
            )
            return dict(native_run=True, reentry=False, result=result)
        except Exception as exc:
            original = getattr(getattr(exc, "error", None), "exc_val", exc)
            atomic(
                root / "failure.json",
                dict(
                    identity=identity,
                    type=type(original).__name__,
                    message=str(original),
                    actual_real_case_run=False,
                ),
            )
            raise
        finally:
            host.closed = True
