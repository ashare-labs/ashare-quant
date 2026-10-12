"""Owner-only current receipts to forward contracts. No caller supplied live facts."""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ashare_quant.daily_portfolio import signals
from ashare_quant.m2_jobs.files import digest, regular, sha

from .online_contracts import OnlineClose, OnlineDecision, OpeningInput
from .risk import require

UNIVERSE = ("600000.XSHG", "600036.XSHG", "601318.XSHG")


@dataclass(frozen=True)
class OwnerReader:
    python: str
    source: Path
    daily_store: Path
    phase_store: Path

    def pin(self):
        package = self.source / "ashare_data"
        evidence = package / "pilot_evidence"
        inventory = {
            str(p.relative_to(self.source)): sha(regular(p))
            for p in sorted(
                set(package.rglob("*.py")) | {p for p in evidence.rglob("*") if p.is_file()}
            )
        }
        require(inventory, "PAPER_OWNER_READER_MISSING")
        return digest(inventory)

    def read(self, config, kind, capture):
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        module = "daily_observation" if kind == "daily" else "phase_observation"
        root = self.daily_store if kind == "daily" else self.phase_store
        boot = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);"
            f"from ashare_data.{module} import read;"
            "print(json.dumps(read(sys.argv[2],sys.argv[3]),ensure_ascii=False))"
        )
        result = subprocess.run(
            [
                self.python,
                "-I",
                "-B",
                "-c",
                boot,
                str(self.source.resolve()),
                str(root.resolve()),
                capture,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        require(result.returncode == 0, "PAPER_OWNER_READ_REJECTED", result.stderr[-3000:])
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        return json.loads(result.stdout)


def scoped_action_effect(state, fact, day, *, require_binding=False):
    """Zero pre-genesis entitlement is distinct from absent corporate actions.

    The owner supplies its finite issuer review, refreshed in the real pre-open
    window. Native genesis and prior decision receipts bind the entitlement and
    review continuity; a zero balance cannot create evidence of absent actions.
    """
    if fact["actions_known_absent"] is True and not require_binding:
        return "none", {"basis": "owner_actions_known_absent"}
    scope = fact.get("action_scope", {})
    review = scope.get("price_share_code_review")
    require(
        isinstance(review, dict)
        and review.get("status") == "proven"
        and review.get("source_sha256")
        and len(review["source_sha256"]) == 64
        and scope.get("from_day", "9999") <= day <= scope.get("through_day", ""),
        "PAPER_ACTION_SCOPE_REVIEW_MISSING",
    )
    genesis = min(state.get("days", {}), default=day)
    if require_binding:
        require(review.get("baseline_sha256") is not None, "PAPER_ACTION_BASELINE_REQUIRED")
    if review.get("baseline_sha256") is not None:
        require(
            scope.get("security") == fact["security"]
            and review.get("security") == fact["security"]
            and review.get("day") == day
            and scope["from_day"] <= genesis <= day
            and len(review["baseline_sha256"]) == 64,
            "PAPER_ACTION_SCOPE_IDENTITY",
        )
        proof = {k: v for k, v in review.items() if k not in {"status", "source_sha256"}}
        require(review["source_sha256"] == digest(proof), "PAPER_ACTION_PROOF_CHANGED")
        # A later day cannot silently adopt a different reviewed corpus. Changes
        # require an explicit reviewed migration, never a hand-edited receipt.
        for prior in state.get("days", {}).values():
            old = prior["decision"]["strategy_receipt"]["action_adjudications"][fact["security"]]
            require(
                old["genesis"] == genesis
                and old["action_scope"]["price_share_code_review"]["baseline_sha256"]
                == review["baseline_sha256"],
                "PAPER_ACTION_BASELINE_CHANGED",
            )
    inventory = fact.get("cash_inventory", {})
    events = inventory.get("events", [])
    require(
        inventory.get("cash_history_complete") is True
        and not inventory.get("unresolved")
        and events
        and all(
            e["type"] == "cash_dividend" and e["record_date"] < genesis and e["ex_date"] < day
            for e in events
        ),
        "PAPER_ACTION_ENTITLEMENT_UNSUPPORTED",
    )
    return "zero_entitlement", dict(
        basis="record_date_before_native_account_genesis",
        genesis=genesis,
        events=events,
        action_scope=scope,
    )


def decision_from_owner(config, state, previous, reader, capture, admission):
    daily = reader.read(config, "daily", capture)
    facts = reader.read(config, "admission", admission)
    require(
        tuple(config.universe) == UNIVERSE and set(daily["securities"]) == set(UNIVERSE),
        "PAPER_OBSERVATION_UNIVERSE",
    )
    require(
        config.strategy_version == sha(regular(Path(signals.__file__))), "PAPER_STRATEGY_IDENTITY"
    )
    require(daily["signal_ready"] and not daily["revisions"], "PAPER_OBSERVATION_REVISION")
    day = daily["calendar"]["next_open"]
    logical = datetime.fromisoformat(day + "T09:00:00+08:00")
    require(
        facts["kind"] == "admission"
        and facts["day"] == day
        and datetime.fromisoformat(facts["completed_at"]) <= logical
        and datetime.fromisoformat(daily["completed_at"]) <= logical,
        "PAPER_ADMISSION_NOT_VISIBLE",
    )
    assessment = facts["assessment"]
    require(
        assessment["execution_permission"] is True and not assessment["blockers"],
        "PAPER_CURRENT_ADMISSION_BLOCKED",
        json.dumps(assessment["blockers"], ensure_ascii=False),
    )
    mapping = {r["security"]: r for r in assessment["rows"]}
    require(set(mapping) == set(UNIVERSE), "PAPER_SECURITY_SET")
    prior_account = previous["native"]["account"] if previous else None
    equity = Decimal(prior_account["nav"] if prior_account else config.initial_cash)
    history = {s: [r["close"] for r in daily["securities"][s]["rows"]] for s in UNIVERSE}
    scheduled = signals.scheduled("equal-weight", day, daily["signal_day"], previous is None)
    signal = signals.decide("equal-weight", history, equity) if scheduled else None
    targets = (
        signal["targets"]
        if scheduled
        else {s: prior_account["positions"][s]["quantity"] for s in UNIVERSE}
    )
    receipt = dict(
        strategy_id="equal-weight",
        strategy_sha256=config.strategy_version,
        equity_from="previous_native_checkpoint" if previous else "declared_initial_capital",
        equity=str(equity),
        parent=state["head"],
        signal_capture=capture,
        admission_capture=admission,
        scheduled=scheduled,
        signal=signal,
        targets=targets,
    )
    rows = []
    receipt["action_adjudications"] = {}
    for sid in UNIVERSE:
        item, fact = daily["securities"][sid], mapping[sid]
        action_effect, action_evidence = scoped_action_effect(
            state, fact, day, require_binding=config.data_mode == "calendar_observed"
        )
        receipt["action_adjudications"][sid] = action_evidence
        if fact.get("reference_close") is not None:
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
                actions_known_absent=fact["actions_known_absent"],
                action_effect=action_effect,
                facts_available_at=facts["completed_at"],
            )
        )
    return OnlineDecision.model_validate(
        dict(
            trading_date=day,
            previous_open=daily["signal_day"],
            next_open=next(d for d in daily["calendar"]["open_days"] if d > day),
            logical_at=logical,
            received_at=datetime.now(timezone.utc),
            calendar_available_at=daily["calendar"]["source"]["observed_at"],
            decision_deadline=day + "T09:05:00+08:00",
            expected_parent=state["head"],
            strategy_version=config.strategy_version,
            strategy_receipt_sha256=digest(receipt),
            data_manifest_sha256=digest([capture, admission]),
            evidence_kind="live_local_capture",
            clock_basis="wall_clock",
            strategy_receipt=receipt,
            rows=rows,
            targets=targets,
            reason="prior-close equal-weight schedule; hold native quantities between monthly rebalances",
        )
    )


def execution_from_owner(config, row, reader, phase, capture):
    require(phase in {"opening", "closing"}, "PAPER_PHASE")
    packet = reader.read(config, "opening" if phase == "opening" else "daily", capture)
    common = dict(
        trading_date=row["day"],
        intent_id=row["intent_id"],
        received_at=packet["completed_at"],
        evidence_kind="live_local_capture",
        clock_basis="wall_clock",
    )
    if phase == "opening":
        require(
            packet["kind"] == "opening"
            and packet["day"] == row["day"]
            and packet["assessment"]["opening_ready"],
            "PAPER_OPENING_NOT_OBSERVED",
        )
        rows = [{k: v for k, v in r.items() if k != "ready"} for r in packet["assessment"]["rows"]]
        return OpeningInput.model_validate(dict(common, rows=rows))
    require(
        packet["signal_day"] == row["day"] and packet["signal_ready"] and not packet["revisions"],
        "PAPER_OBSERVATION_REVISION",
    )
    rows = []
    for sid, item in packet["securities"].items():
        last = item["rows"][-1]
        rows.append(
            dict(
                security=sid,
                trading_date=row["day"],
                **{k: last[k] for k in ("open", "high", "low", "close")},
                volume=int(last["volume"]),
                observed_at=item["source"]["observed_at"],
                complete=True,
                source_watermark=row["day"],
                selected_watermark=row["day"],
            )
        )
    return OnlineClose.model_validate(
        dict(
            common, rows=rows, data_manifest_sha256=capture, deadline=row["day"] + "T18:45:00+08:00"
        )
    )
