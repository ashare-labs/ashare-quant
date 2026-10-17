"""Pinned local research inputs -> shared signals -> the existing paper ledger.

Only the local CLI imports files. HTTP cannot choose paths, code or data providers.
The archived full window is a signal input; the native worker still receives one
new day and the previous native checkpoint, never a rerun from an empty account.
"""

import json
from pathlib import Path

import ashare_quant.portfolio.signals as signals
from ashare_quant.contracts import DomainError
from ashare_quant.paper.contracts import CloseInput, Decision, RiskPolicy, SessionConfig
from ashare_quant.paper.risk import require
from ashare_quant.portfolio.inputs import Inputs
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, sha


def strategy_version(manifest, strategy):
    spec = next((s for s in manifest["strategies"] if s["id"] == strategy), None)
    require(spec is not None, "PAPER_STRATEGY_UNKNOWN")
    return digest(
        {
            "shared_signals_sha256": sha(Path(signals.__file__).read_bytes()),
            "strategy": spec,
            "execution": manifest["execution"],
        }
    )


def bind(
    service,
    manifest_path,
    input_path,
    strategy,
    window,
    account,
    *,
    accept_model=False,
    risk=None,
    cash_dividend_policy="reject",
):
    """Freeze copies and config. Repeating an identical import is safe."""
    manifest_raw, input_raw = regular(Path(manifest_path)), regular(Path(input_path))
    manifest = json.loads(manifest_raw)
    scope = next((w for w in manifest["windows"] if w["id"] == window), None)
    require(scope is not None, "PAPER_WINDOW_UNKNOWN")
    inputs = Inputs(input_path, manifest, scope)
    doc = inputs.document
    if doc["real_market_data"]:
        require(
            accept_model,
            "PAPER_RESEARCH_OPT_IN_REQUIRED",
            "AD research data requires explicit retrospective state-model acceptance.",
        )
        require(doc.get("state_mode") == "retrospective_model", "PAPER_OWNER_STATE_UNKNOWN")
        owner = doc.get("owner_descriptor", {})
        require(
            owner.get("execution_permission") is False
            and owner.get("historical_pit") is False
            and owner.get("role") == "daily_strategy_retrospective_research",
            "PAPER_RESEARCH_ROLE_REQUIRED",
        )
    binding = {
        "schema": "paper-replay-binding.v1",
        "manifest_sha256": sha(manifest_raw),
        "input_sha256": sha(input_raw),
        "strategy_id": strategy,
        "window": scope,
        "accept_owner_state_model": accept_model,
        "real_market_data": doc["real_market_data"],
        "cash_dividend_policy": cash_dividend_policy,
    }
    execution = manifest["execution"]
    require(
        execution["gross_target_weight"] == "0.98" and execution["lot_size"] == 100,
        "PAPER_FROZEN_STRATEGY_PARAMETERS",
    )
    fees = execution["fees"]
    require(
        fees["buy_commission_rate"] == fees["sell_commission_rate"]
        and fees["slippage"] == fees["transfer_fee"] == "0",
        "PAPER_FEE_MODEL_UNSUPPORTED",
    )
    config = SessionConfig(
        account_id=account,
        strategy_id=strategy,
        strategy_version=strategy_version(manifest, strategy),
        scenario_manifest_sha256=sha(manifest_raw),
        replay_binding_sha256=digest(binding),
        initial_cash=execution["initial_cash"],
        universe=tuple(manifest["securities"]),
        data_mode="historical_research" if doc["real_market_data"] else "synthetic_fixture",
        commission_rate=fees["buy_commission_rate"],
        minimum_commission=fees["minimum_commission"],
        sell_tax_rate=fees["sell_tax_rate"],
        risk=RiskPolicy.model_validate(risk or {}),
        cash_dividend_policy=cash_dividend_policy,
    )
    session = service.create(config)
    root = service.root / session["id"] / "replay"
    root.mkdir(exist_ok=True)
    with lock(service.root / session["id"] / "import.lock"):
        for name, raw in (
            ("manifest.json", manifest_raw),
            ("input.json", input_raw),
            ("binding.json", canonical(binding)),
        ):
            path = root / name
            if path.exists():
                require(regular(path) == raw, "PAPER_REPLAY_INPUT_CHANGED")
            else:
                atomic(path, raw)
    return {
        "session_id": session["id"],
        "identity": session["identity"],
        "binding": binding,
        "preflight": inputs.preflight(),
    }


def load(service, sid, config):
    root = service.root / sid / "replay"
    require(
        (root / "binding.json").is_file(),
        "PAPER_REPLAY_BINDING_MISSING",
        "Complete the local CLI import before advancing this session.",
    )
    binding = json.loads(regular(root / "binding.json"))
    require(digest(binding) == config.replay_binding_sha256, "PAPER_REPLAY_BINDING_CHANGED")
    if binding["schema"] == "paper-execution-binding.v1":
        from ashare_quant.paper.execution import ExecutionInputs

        manifest_raw = regular(root / "manifest.json")
        require(
            sha(manifest_raw) == binding["manifest_sha256"] == config.scenario_manifest_sha256,
            "PAPER_REPLAY_INPUT_CHANGED",
        )
        require(
            strategy_version(json.loads(manifest_raw), config.strategy_id)
            == config.strategy_version,
            "PAPER_STRATEGY_IDENTITY",
        )
        return binding, ExecutionInputs(service, sid, binding)
    manifest_raw, input_raw = regular(root / "manifest.json"), regular(root / "input.json")
    require(
        sha(manifest_raw) == binding["manifest_sha256"] == config.scenario_manifest_sha256
        and sha(input_raw) == binding["input_sha256"],
        "PAPER_REPLAY_INPUT_CHANGED",
    )
    manifest = json.loads(manifest_raw)
    require(
        strategy_version(manifest, config.strategy_id) == config.strategy_version,
        "PAPER_STRATEGY_IDENTITY",
    )
    return binding, Inputs(root / "input.json", manifest, binding["window"])


def provenance(binding, inputs, *, through_day=None):
    doc = inputs.document
    result = {
        "binding": binding,
        "producer": doc["producer"],
        "source_refs": doc["source_refs"],
        "owner_binding_sha256": doc.get("owner_descriptor", {}).get("binding_sha256"),
        "state_mode": doc.get("state_mode", "synthetic_known"),
        "limitations": doc["limitations"],
        "assumptions": doc.get("assumptions", []),
        "clock_basis": "retrospective_model" if doc["real_market_data"] else "synthetic_clock",
        "historical_pit_verified": False,
        "execution_permission": False,
        "preflight": inputs.preflight(),
    }
    if hasattr(inputs, "deliveries"):
        received = inputs.deliveries()
        # A daily intent must not change when a later input is appended. Keep
        # live views current, but freeze receipt provenance at its trading day.
        if through_day is not None:
            received = [entry for entry in received if entry["day"] <= through_day]
        result["delivery"] = {
            "mode": "sealed_historical_source_increment",
            "received": received,
            "next_input_day": inputs.days[len(received)]
            if len(received) < len(inputs.days)
            else None,
            "source_sha256": binding["source_sha256"],
            "ad_admission": inputs.registry["description"]["admission"],
            "observation_admission": inputs.observation_admission,
            "consumer_contract": inputs.registry["spec"].get("consumer_contract"),
        }
    return result


def build(service, sid, day, parent):
    _, config = service.session(sid)
    binding, inputs = load(service, sid, config)
    require(day in inputs.days, "PAPER_REPLAY_DATE_OUT_OF_SCOPE")
    if hasattr(inputs, "select"):
        inputs.select(day)
    require(
        not inputs.gaps,
        "PAPER_REPLAY_DATA_MISSING",
        "Owner export has missing fields: " + json.dumps(inputs.gaps[:12], ensure_ascii=False),
    )
    # Record-day trading needs no posting. At the posting date an immutable
    # record-day native account can prove zero entitlement; no action is invented.
    action_checks, cash_dividends = [], []
    zero_entitlement = set()
    for event in inputs.actions:
        dates = [v for k, v in event.items() if k.endswith("_date") and isinstance(v, str)]
        if config.cash_dividend_policy == "gross_zero_withholding_0900":
            require(event["kind"] == "cash_dividend", "PAPER_ACTION_TIMING_UNSUPPORTED")
            if not event["ex_date"] <= day <= event["pay_date"]:
                continue
            record = service.report(sid, event["record_date"])
            cash_dividends.append(
                {
                    **{
                        k: event[k]
                        for k in (
                            "security",
                            "record_date",
                            "ex_date",
                            "pay_date",
                            "cash_per_share",
                        )
                    },
                    "record_quantity": record["native"]["account"]["positions"][event["security"]][
                        "quantity"
                    ],
                    "record_checkpoint": record["checkpoint_sha256"],
                    "source_event": event.get("source_event", event),
                    "model_cash_available_at": event["pay_date"] + "T09:00:00+08:00",
                }
            )
            action_checks.append(
                {
                    **cash_dividends[-1],
                    "effect": "native_cash_dividend",
                    "withholding_rate": "0",
                    "policy": config.cash_dividend_policy,
                }
            )
            continue
        if config.risk.profile == "conservative_v1":
            require(
                not min(dates) <= day <= max(dates),
                "PAPER_ACTION_TIMING_UNSUPPORTED",
                f"{event['security']} {event['kind']} {min(dates)}..{max(dates)}: "
                "continuous native action/receivable recovery is not admitted.",
            )
            continue
        if not event["ex_date"] <= day <= max(dates):
            continue
        if config.risk.profile == "target_then_native_cash_v1":
            record = service.report(sid, event["record_date"])
            quantity = record["native"]["account"]["positions"][event["security"]]["quantity"]
            if quantity == 0:
                zero_entitlement.add(event["security"])
                action_checks.append(
                    {
                        "security": event["security"],
                        "kind": event["kind"],
                        "record_date": event["record_date"],
                        "record_checkpoint": record["checkpoint_sha256"],
                        "record_quantity": 0,
                        "effect": "zero_entitlement",
                    }
                )
                continue
        require(
            False,
            "PAPER_ACTION_TIMING_UNSUPPORTED",
            f"{event['security']} {event['kind']} {event['record_date']}..{max(dates)}: "
            "positive/unknown record entitlement requires native action/receivable continuation.",
        )
    prior = inputs.previous[day]
    if day == inputs.days[0]:
        require(parent is None, "PAPER_GENESIS_PARENT")
        equity, quantities = config.initial_cash, dict.fromkeys(config.universe, 0)
    else:
        previous = service.report(sid, prior)
        require(previous["checkpoint_sha256"] == parent, "PAPER_PARENT_CONFLICT")
        account = previous["native"]["account"]
        equity = account["nav"]
        quantities = {s: p["quantity"] for s, p in account["positions"].items()}
    history = inputs.history(day)
    scheduled = signals.scheduled(config.strategy_id, day, prior, day == inputs.days[0])
    signal = (
        signals.decide(config.strategy_id, history, equity)
        if scheduled
        else {
            "selected": [],
            "scores": {},
            "weights": {},
            "targets": quantities,
        }
    )
    order_permissions = (
        inputs.order_permissions(day, signal["targets"], quantities)
        if hasattr(inputs, "order_permissions")
        else []
    )
    receipt = {
        "schema": "paper-strategy-receipt.v1",
        "strategy_id": config.strategy_id,
        "strategy_version": config.strategy_version,
        "manifest_sha256": config.scenario_manifest_sha256,
        "input_sha256": inputs.sha256,
        "window_id": binding["window"]["id"],
        "day": day,
        "information_cutoff": prior + "T15:00:00+08:00",
        "history_sha256": digest(history),
        "history_sessions": 120,
        "previous_equity": equity,
        "parent_checkpoint": parent,
        "scheduled": scheduled,
        "reason": "scheduled_rebalance" if scheduled else "hold_actual_positions_no_retry",
        "signal": signal,
        "provenance": provenance(binding, inputs, through_day=day),
        "action_checks": action_checks,
        "input_delivery": (
            {
                "packet_sha256": inputs.sha256,
                "admission": inputs.packet["admission"],
                "reads": inputs.packet["reads"],
            }
            if hasattr(inputs, "packet")
            else None
        ),
    }
    historical = config.data_mode == "historical_research"
    mode = {
        "evidence_kind": "historical_research" if historical else "synthetic",
        "clock_basis": "retrospective_model" if historical else "synthetic_clock",
    }
    # These are declared MODEL availability times, not source receipt timestamps.
    # Original owner provenance remains in the receipt and immutable input file.
    rows = []
    for security in config.universe:
        row = inputs.row(security, day)
        rows.append(
            dict(
                security=security,
                price_date=prior,
                previous_close=str(history[security][-1]),
                observed_at=prior + "T16:00:00+08:00",
                complete=True,
                source_watermark=prior,
                selected_watermark=prior,
                eligible=row["eligible"] if hasattr(inputs, "packet") else True,
                suspended=row["suspended"],
                is_st=row["is_st"] if hasattr(inputs, "packet") else False,
                lower_limit=str(row["lower_limit"]) if row["lower_limit"] is not None else None,
                upper_limit=str(row["upper_limit"]) if row["upper_limit"] is not None else None,
                rule_version="explicit-owner-research-model" if historical else "synthetic-rules",
                actions_known_absent=security not in zero_entitlement
                and not any(a["security"] == security for a in cash_dividends),
                action_effect=(
                    "native_cash_dividend"
                    if any(a["security"] == security for a in cash_dividends)
                    else "zero_entitlement"
                    if security in zero_entitlement
                    else "none"
                ),
                facts_available_at=day + "T09:00:00+08:00",
            )
        )
    decision = Decision(
        trading_date=day,
        previous_open=prior,
        next_open=inputs.calendar[inputs.calendar.index(day) + 1],
        logical_at=day + "T09:00:00+08:00",
        received_at=day + "T09:00:01+08:00",
        calendar_available_at=prior + "T16:00:00+08:00",
        decision_deadline=day + "T09:05:00+08:00",
        expected_parent=parent,
        strategy_version=config.strategy_version,
        strategy_receipt_sha256=digest(receipt),
        strategy_receipt=receipt,
        cash_dividends=cash_dividends,
        order_admissions=order_permissions,
        data_manifest_sha256=inputs.sha256,
        rows=rows,
        targets=signal["targets"],
        reason=receipt["reason"],
        **mode,
    )
    return decision, inputs


def close_for(config, decision, inputs, intent):
    day = decision.trading_date.isoformat()
    return CloseInput(
        trading_date=day,
        intent_id=intent,
        data_manifest_sha256=inputs.sha256,
        received_at=day + "T16:10:00+08:00",
        deadline=day + "T18:00:00+08:00",
        evidence_kind=decision.evidence_kind,
        clock_basis=decision.clock_basis,
        rows=[
            dict(
                security=s,
                trading_date=day,
                **{k: str(inputs.row(s, day)[k]) for k in ("open", "high", "low", "close")},
                volume=int(inputs.row(s, day)["volume"]),
                observed_at=day + "T16:00:00+08:00",
                complete=True,
                source_watermark=day,
                selected_watermark=day,
            )
            for s in config.universe
        ],
    )


def advance(service, sid, day, parent, *, fault=None):
    try:
        decision, inputs = build(service, sid, day, parent)
        row = service.prepare(sid, decision)
        _, config = service.session(sid)
        service.seal_close(sid, close_for(config, decision, inputs, row["intent_id"]))
        return service.work(sid, day, fault=fault)
    except DomainError as exc:
        service.alert(sid, day, exc)
        raise
