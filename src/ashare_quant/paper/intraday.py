"""Owner-only adapter for the independent intraday paper identity."""

import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ashare_quant.daily_portfolio import signals
from ashare_quant.m2_jobs.files import digest, sha

from .intraday_contracts import IntradayPlan, continuous
from .online_adapter import OwnerReader, scoped_action_effect
from .online_contracts import OnlineDecision, OnlineSession, OpeningInput
from .risk import profiles, require


def now():
    return datetime.now(timezone.utc)


class IntradayReader(OwnerReader):
    def intraday(self, config, capture):
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        boot = "import sys,json;sys.path.insert(0,sys.argv[1]);from ashare_data.intraday_observation import read;print(json.dumps(read(sys.argv[2],sys.argv[3])))"
        r = subprocess.run(
            [
                self.python,
                "-I",
                "-B",
                "-c",
                boot,
                str(self.source.resolve()),
                str(self.phase_store.resolve()),
                capture,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        require(r.returncode == 0, "PAPER_OWNER_READ_REJECTED", r.stderr[-3000:])
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        return json.loads(r.stdout)


def config_for(plan, account):
    plan = IntradayPlan.model_validate(plan)
    return OnlineSession(
        account_id=account,
        model=plan.model,
        data_mode="calendar_observed",
        initial_cash=plan.initial_cash,
        universe=plan.universe,
        strategy_version=sha(Path(signals.__file__).read_bytes()),
        scenario_manifest_sha256=digest(plan),
        admission_reader_sha256=plan.admission_reader_sha256,
        risk=profiles()["target_then_native_cash_v1"],
    )


def prepare(service, reader, plan, admission):
    plan = IntradayPlan.model_validate(plan)
    state, config = service._load()
    continuation = state.get("continuation")
    successor = continuation is not None and str(plan.day) == continuation["plan"]["day"]
    if successor:
        from .intraday_continuation import manifest_gate

        extension = manifest_gate(continuation, config)
        require(plan == extension.plan, "PAPER_CONTINUATION_PLAN_CHANGED")
    else:
        require(config.scenario_manifest_sha256 == digest(plan), "PAPER_INTRADAY_PLAN_IDENTITY")
    daily = reader.read(config, "daily", plan.daily_capture)
    facts = reader.intraday(config, admission)
    old = state["days"].get(str(plan.day))
    if old:
        receipt = old["decision"]["strategy_receipt"]
        require(
            receipt["admission_capture"] == admission
            and receipt["signal_capture"] == plan.daily_capture,
            "PAPER_IDEMPOTENCY_CONFLICT",
        )
        return old
    if successor:
        require(
            state["last_day"] == str(extension.origin_plan.day)
            and state["head"] == extension.parent_checkpoint,
            "PAPER_CONTINUATION_PARENT_CHANGED",
        )
    else:
        require(state["days"] == {} and state["head"] is None, "PAPER_INTRADAY_NEW_ACCOUNT")
    at = now()
    logical = datetime.fromisoformat(facts["completed_at"])
    require(
        plan.start_at <= logical <= at <= plan.intent_deadline
        and continuous(at)
        and facts["not_before"] == plan.start_at.isoformat()
        and facts["kind"] == "admission"
        and facts["day"] == str(plan.day)
        and facts["assessment"]["ready"]
        and not facts["assessment"]["blockers"],
        "PAPER_INTRADAY_ADMISSION_BLOCKED",
    )
    require(
        daily["signal_ready"]
        and daily["revisions"] == []
        and daily["calendar"]["next_open"] == str(plan.day)
        and datetime.fromisoformat(daily["completed_at"]) <= logical,
        "PAPER_INTRADAY_PRIOR_SIGNAL",
    )
    previous = service.report(state["last_day"], "closing") if successor else None
    account = previous["native"]["account"] if previous else None
    scheduled = signals.scheduled("equal-weight", str(plan.day), daily["signal_day"], not successor)
    if successor:
        require(not scheduled, "PAPER_CONTINUATION_REBALANCE_REQUIRED")
    signal = (
        signals.decide(
            "equal-weight",
            {s: [r["close"] for r in daily["securities"][s]["rows"]] for s in config.universe},
            Decimal(account["nav"] if account else config.initial_cash),
        )
        if scheduled
        else None
    )
    targets = (
        signal["targets"]
        if scheduled
        else {s: account["positions"][s]["quantity"] for s in config.universe}
    )
    receipt = dict(
        strategy_id="equal-weight",
        signal=signal,
        signal_capture=plan.daily_capture,
        admission_capture=admission,
        intraday_plan=plan.model_dump(mode="json"),
        action_adjudications={},
        model="new-account prior-close equal-weight; future complete-minute execution",
    )
    if successor:
        receipt.update(
            model="same native account; monthly equal-weight holds actual quantities",
            continuation=continuation,
            scheduled=False,
            targets=targets,
            equity_from="previous_native_checkpoint",
            equity=account["nav"],
            parent=state["head"],
        )
    rows = []
    for fact in facts["assessment"]["rows"]:
        sid = fact["security"]
        item = daily["securities"][sid]
        proof = fact["action_scope"]["price_share_code_review"]
        require(
            proof["baseline_sha256"] == plan.action_baseline_sha256, "PAPER_ACTION_BASELINE_CHANGED"
        )
        effect, evidence = scoped_action_effect(state, fact, str(plan.day), require_binding=True)
        receipt["action_adjudications"][sid] = evidence
        require(
            Decimal(fact["reference_close"]) == Decimal(item["rows"][-1]["close"]),
            "PAPER_REFERENCE_CLOSE_CHANGED",
        )
        rows.append(
            dict(
                security=sid,
                price_date=daily["signal_day"],
                previous_close=item["rows"][-1]["close"],
                observed_at=item["source"]["observed_at"],
                complete=True,
                source_watermark=daily["signal_day"],
                selected_watermark=daily["signal_day"],
                eligible=fact["eligible"],
                suspended=fact["suspended"],
                is_st=fact["is_st"],
                lower_limit=fact["lower_limit"],
                upper_limit=fact["upper_limit"],
                rule_version=fact["rule_version"],
                actions_known_absent=False,
                action_effect=effect,
                facts_available_at=facts["completed_at"],
            )
        )
    d = OnlineDecision(
        trading_date=plan.day,
        previous_open=daily["signal_day"],
        next_open=next(d for d in daily["calendar"]["open_days"] if d > str(plan.day)),
        logical_at=logical,
        received_at=at,
        calendar_available_at=daily["calendar"]["source"]["observed_at"],
        decision_deadline=plan.intent_deadline,
        expected_parent=state["head"] if successor else None,
        strategy_version=config.strategy_version,
        strategy_receipt=receipt,
        strategy_receipt_sha256=digest(receipt),
        data_manifest_sha256=digest([plan.daily_capture, admission]),
        rows=rows,
        targets=targets,
        reason="Prior close strategy intent, future minute quote required",
        evidence_kind="live_local_capture",
        clock_basis="wall_clock",
    )
    return service._prepare(d)


def deliver(service, reader, plan, capture):
    plan = IntradayPlan.model_validate(plan)
    state, config = service._load()
    row = state["days"][str(plan.day)]
    p = reader.intraday(config, capture)
    decision = OnlineDecision.model_validate(row["decision"])
    require(
        p["kind"] == "execution"
        and p["day"] == str(plan.day)
        and datetime.fromisoformat(p["not_before"]) == decision.received_at
        and p["assessment"]["ready"]
        and not p["assessment"]["blockers"],
        "PAPER_INTRADAY_EXECUTION_BLOCKED",
    )
    prices = []
    facts = []
    minutes = []
    for r in p["assessment"]["rows"]:
        e = r["execution"]
        sid = r["security"]
        review = r["action_scope"]["price_share_code_review"]
        prices.append(
            dict(
                security=sid,
                trading_date=plan.day,
                open=e["match_price"],
                observed_at=e["observed_at"],
                source_as_of=e["minute_end"],
                raw_sha256=e["source_sha256"],
            )
        )
        facts.append(
            dict(
                **{
                    k: r[k]
                    for k in (
                        "security",
                        "eligible",
                        "suspended",
                        "is_st",
                        "reference_close",
                        "lower_limit",
                        "upper_limit",
                        "rule_version",
                    )
                },
                action_baseline_sha256=review["baseline_sha256"],
                action_review_sha256=review["source_sha256"],
            )
        )
        minutes.append(dict(security=sid, **e))
    value = OpeningInput(
        trading_date=plan.day,
        intent_id=row["intent_id"],
        received_at=p["completed_at"],
        evidence_kind="live_local_capture",
        clock_basis="wall_clock",
        rows=prices,
        execution_admission=dict(
            schema_version=p["schema"],
            capture_id=capture,
            observed_at=p["completed_at"],
            rows=facts,
        ),
        intraday=dict(
            plan_sha256=digest(plan), intent_received_at=decision.received_at, rows=minutes
        ),
    )
    return service._deliver("opening", value)
