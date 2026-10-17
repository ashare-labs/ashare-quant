"""Two-stage checkpoint extension, not a second trading ledger or file writer.

Returned states are candidates: only the existing native atomic checkpoint may
commit them alongside the native book/cursor. No API exposes these transitions.
"""

import json
from datetime import datetime, time
from typing import Any, Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from ashare_quant.backtests.daily.catalogue import PreparationEntry, check_context
from ashare_quant.backtests.daily.contracts import (
    AccountInput,
    BacktestRequest,
    DecisionContext,
    ReadEvidence,
    Sha,
    Signal,
    StrategyVersion,
)
from ashare_quant.backtests.daily.execution_dates import execution_days
from ashare_quant.backtests.daily.facade import validate_evidence
from ashare_quant.contracts import SHANGHAI, Contract, DomainError, Identifier
from ashare_quant.storage.canonical import canonical, digest, require
from ashare_quant.strategies.daily.registry import StrategyRegistry
from ashare_quant.strategies.daily.strategy import evaluate
from ashare_quant.strategies.python.decision import PythonDecisionReceipt
from ashare_quant.strategies.python.decision import verify as verify_python_receipt
from ashare_quant.strategies.python.spec import is_python
from ashare_quant.strategies.python.spec import parameters as check_python_parameters


class Session(Contract):
    run_id: Identifier
    request: BacktestRequest
    strategy: StrategyVersion
    entry: PreparationEntry
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)
    data_binding_sha256: Sha | None = None

    @model_validator(mode="after")
    def links(self):
        StrategyRegistry._decode(canonical(self.strategy), self.request.strategy_version_id)
        if is_python(self.strategy):
            check_python_parameters(self.strategy_parameters)
            require(self.data_binding_sha256 is not None, "PYTHON_INPUT_INVALID", "Frozen local data binding required")
        else:
            require(not self.strategy_parameters, "PYTHON_PARAMETERS_UNEXPECTED", "Only Python tasks have Python parameters")
        require(self.request.data_binding_id == self.entry.binding.binding_id,
                "M2_SESSION_MISMATCH", "Session data binding differs")
        require(self.request.assumptions.profile_sha256 == self.entry.profile(self.request.window_id).profile_sha256,
                "M2_SESSION_MISMATCH", "Session assumption profile differs")
        execution_days(self.entry, self.request)
        require(not self.entry.unmet_requirements, "M2_SESSION_MISMATCH", "Unmet data requirements")
        if self.entry.evidence_kind == "observed":
            from ashare_quant.providers.daily.owner_wire import validate_binding
            validate_binding(self.entry)
        return self

    @property
    def days(self):
        return execution_days(self.entry, self.request)

    @property
    def identity(self):
        return digest(self)


class DecisionRecord(Contract):
    stage: Literal["decision_committed_pending_submit"] = "decision_committed_pending_submit"
    decision_id: Sha
    economic_input_sha256: Sha
    commit_sequence: int = Field(strict=True, ge=1)
    cursor_after: int = Field(strict=True, ge=1)
    context: DecisionContext
    account: AccountInput
    read_evidence: ReadEvidence | None
    signal: Signal
    python_receipt: PythonDecisionReceipt | None = None


class NativeOrderProjection(Contract):
    order_id: Identifier
    security: Literal["600000.XSHG"] = "600000.XSHG"
    side: Literal["buy", "sell"]
    requested_quantity: int = Field(strict=True, gt=0)
    filled_quantity: int = Field(strict=True, ge=0)
    remaining_quantity: int = Field(strict=True, ge=0)
    status: Literal["filled", "partially_filled", "rejected", "cancelled", "active"]
    trade_ids: tuple[Identifier, ...]
    reason: str | None = None

    @model_validator(mode="after")
    def consistent_projection(self):
        if self.filled_quantity + self.remaining_quantity != self.requested_quantity:
            raise ValueError("Native requested/filled/remaining projection disagrees")
        if bool(self.filled_quantity) != bool(self.trade_ids) or len(set(self.trade_ids)) != len(self.trade_ids):
            raise ValueError("Filled quantity requires distinct native trade references")
        if self.status == "filled" and self.remaining_quantity != 0:
            raise ValueError("Filled order has remaining quantity")
        if self.status == "partially_filled" and not 0 < self.filled_quantity < self.requested_quantity:
            raise ValueError("Partial order must preserve both filled and remaining")
        if self.status == "rejected" and self.filled_quantity:
            raise ValueError("Rejected order cannot have fills")
        return self


class ExecutionOutcome(Contract):
    stage: Literal["execution_committed"] = "execution_committed"
    decision_id: Sha
    commit_sequence: int = Field(strict=True, ge=1)
    cursor_after: int = Field(strict=True, ge=1)
    execution_at: AwareDatetime
    native_receipt_sha256: Sha
    native_account_sha256: Sha
    orders: tuple[NativeOrderProjection, ...] = Field(max_length=1)
    no_order_reason: Literal["no_change", "already_at_target", "native_rejected_without_order"] | None = None
    native_reason: str | None = None

    @model_validator(mode="after")
    def order_links(self):
        if bool(self.orders) == (self.no_order_reason is not None):
            raise ValueError("No-order outcome must have an explicit reason and no native orders")
        if self.no_order_reason == "native_rejected_without_order" and not self.native_reason:
            raise ValueError("Native refusal requires a reason")
        ids = [o.order_id for o in self.orders]
        trades = [t for o in self.orders for t in o.trade_ids]
        if len(ids) != len(set(ids)) or len(trades) != len(set(trades)):
            raise ValueError("Native order/trade identities must be unique")
        return self


class DayClose(Contract):
    decision_id: Sha
    commit_sequence: int = Field(strict=True, ge=1)
    cursor_after: int = Field(strict=True, ge=1)
    native_receipt_sha256: Sha
    account: AccountInput
    final_orders: tuple[NativeOrderProjection, ...]


class Journal(Contract):
    state_schema: Literal["m2.two_stage.v3"] = "m2.two_stage.v3"
    session: Session
    initial_account: AccountInput
    decisions: tuple[DecisionRecord, ...] = ()
    executions: tuple[ExecutionOutcome, ...] = ()
    day_closes: tuple[DayClose, ...] = ()
    complete: bool = Field(strict=True, default=False)

    @property
    def last_sequence(self):
        return max([0, *[r.commit_sequence for r in (*self.decisions, *self.executions, *self.day_closes)]])

    @property
    def last_cursor(self):
        return max([0, *[r.cursor_after for r in (*self.decisions, *self.executions, *self.day_closes)]])


def account_digest(account):
    return digest(account.model_dump(mode="json", exclude={"snapshot_sha256"}))


def initial_state(session):
    first = session.days[0].trading_date
    body = dict(run_id=session.run_id, account_id="bt-" + session.run_id, source_kind="initial",
                commit_sequence=0, account_as_of=datetime.combine(first, time(9), SHANGHAI).isoformat(),
                cash=session.request.initial_cash, quantity=0, sellable=0)
    account = AccountInput(**body, snapshot_sha256=digest(body))
    return Journal(session=session, initial_account=account)


def economic_input(session, context, account, read):
    # No PID, epoch, generation, request-observation IDs or authority digest here.
    return dict(session_identity=session.identity, trading_date=context.trading_date,
        previous_open=context.previous_open, account_sha256=account.snapshot_sha256,
        value=read.native_value if read else None,
        stable_read_evidence_sha256=read.owner_receipt.evidence_sha256 if read else None,
        owner_stable_documents_sha256=digest(read.owner_documents.stable()) if read and read.owner_documents else None,
        call=read.call if read else None)


def validate_account(account, session):
    require(account.run_id == session.run_id and account.account_id == "bt-" + session.run_id
            and account.snapshot_sha256 == account_digest(account),
            "M2_ACCOUNT_SOURCE_MISMATCH", "Account projection belongs to another run or commit")


def check_inputs(session, context, account, read, expected_account):
    check_context(session.entry, context, run_id=session.run_id, request=session.request)
    validate_account(account, session)
    require(account == expected_account, "M2_ACCOUNT_SOURCE_MISMATCH", "Use the exact prior committed account projection")
    if session.strategy.definition.template in {"previous_close_band.v1", "decision_rules.v1", "trusted_python_decision.v1"}:
        require(read is not None, "M2_READ_EVIDENCE_REQUIRED", "Decision must persist the consumed native read fact")
        validate_evidence(read, session.entry, context,
                          run_id=session.run_id, request=session.request)
    else:
        require(read is None, "M2_UNUSED_PRICE", "Buy-hold does not claim AD08 coverage")


def evaluate_inputs(session, context, account, read):
    require(not is_python(session.strategy), "PYTHON_CALLBACK_REQUIRED", "Python may execute only during a new native decision")
    day_index = next(i for i, day in enumerate(session.days)
                     if day.trading_date == context.trading_date)
    return evaluate(session.strategy.definition, previous_close=read.native_value if read else None,
                    prior_cash=account.cash, prior_quantity=account.quantity, day_index=day_index)


def check_decision(record, state, expected_account):
    session = state.session
    check_inputs(session, record.context, record.account, record.read_evidence, expected_account)
    if is_python(session.strategy):
        signal = verify_python_receipt(record.python_receipt, session, record.context, record.account, record.read_evidence)
    else:
        require(record.python_receipt is None, "PYTHON_RECEIPT_UNEXPECTED", "JSON rules retain pure semantic replay")
        signal = evaluate_inputs(session, record.context, record.account, record.read_evidence)
    require(record.signal == signal, "M2_DECISION_SIGNAL_MISMATCH", "Committed intent differs from its recorded decision")
    economic = digest(economic_input(session, record.context, record.account, record.read_evidence))
    require(record.economic_input_sha256 == economic and record.decision_id == digest({"input": economic, "signal": record.signal}),
            "M2_DECISION_IDENTITY_MISMATCH", "Decision input/intent identity differs")


def prepare_decision(state, context, account, read, *, commit_sequence, cursor_after, python_receipt=None):
    require(not state.complete and len(state.decisions) == len(state.day_closes),
            "M2_DECISION_ALREADY_PENDING", "Finish the committed day before making a new decision")
    expected_account = state.day_closes[-1].account if state.day_closes else state.initial_account
    check_inputs(state.session, context, account, read, expected_account)
    if is_python(state.session.strategy):
        signal = verify_python_receipt(python_receipt, state.session, context, account, read)
    else:
        require(python_receipt is None, "PYTHON_RECEIPT_UNEXPECTED", "JSON rules retain pure semantic replay")
        signal = evaluate_inputs(state.session, context, account, read)
    economic = digest(economic_input(state.session, context, account, read))
    record = DecisionRecord(decision_id=digest({"input": economic, "signal": signal}), economic_input_sha256=economic,
        context=context, account=account, read_evidence=read, signal=signal, python_receipt=python_receipt,
        commit_sequence=commit_sequence, cursor_after=cursor_after)
    candidate = state.model_copy(update={"decisions": (*state.decisions, record)})
    validate_state(candidate)
    return candidate


def pending_for_submit(state, logical_at):
    require(not state.complete and len(state.decisions) == len(state.executions) + 1,
            "M2_NO_PENDING_INTENT", "No unexecuted committed intent")
    record = state.decisions[-1]
    require(logical_at.tzinfo is not None, "M2_SUBMIT_CLOCK", "Aware submit clock required")
    local = logical_at.astimezone(SHANGHAI)
    require(local.date() == record.context.trading_date and local.timetz().replace(tzinfo=None) == time(15),
            "M2_SUBMIT_CLOCK", "Only the same day's 15:00 native bar may consume this intent")
    return record  # No evaluate, query, authorization reuse or order submission.


def prepare_execution(state, outcome):
    require(not state.complete and len(state.decisions) == len(state.executions) + 1,
            "M2_EXECUTION_ALREADY_COMMITTED", "Only one native outcome per pending intent")
    candidate = state.model_copy(update={"executions": (*state.executions, outcome)})
    validate_state(candidate)
    return candidate


def prepare_day_close(state, close):
    require(not state.complete and len(state.executions) == len(state.day_closes) + 1,
            "M2_DAY_CLOSE_ORDER", "A native execution commit must precede day close")
    candidate = state.model_copy(update={"day_closes": (*state.day_closes, close)})
    validate_state(candidate)
    return candidate


def prepare_complete(state):
    require(len(state.day_closes) == len(state.session.days),
            "M2_INCOMPLETE_WINDOW", "Every registered trading day needs a completed native commit")
    candidate = state.model_copy(update={"complete": True})
    validate_state(candidate)
    return candidate


def validate_state(state):
    session = state.session
    days = session.days
    require(state.initial_account == initial_state(session).initial_account,
            "M2_INITIAL_ACCOUNT_MISMATCH", "Initial account must match this run's zero-position declaration")
    require(len(state.day_closes) <= len(state.executions) <= len(state.decisions) <= len(days)
            and len(state.decisions) - len(state.day_closes) <= 1,
            "M2_STAGE_ORDER", "Two-stage record order differs")
    prior_seq, prior_cursor = 0, 0
    previous_account = state.initial_account
    seen_orders, seen_trades = set(), set()
    for i, decision in enumerate(state.decisions):
        require(decision.context.trading_date == days[i].trading_date
                and decision.context.event_cursor >= prior_cursor,
                "M2_DAY_ORDER", "No skipped/reordered decision days or regressed cursor")
        check_decision(decision, state, previous_account)
        records = [decision]
        if i < len(state.executions):
            outcome = state.executions[i]
            require(outcome.decision_id == decision.decision_id, "M2_OUTCOME_LINK", "Execution refers to another decision")
            at = outcome.execution_at.astimezone(SHANGHAI)
            require(at.date() == decision.context.trading_date and at.timetz().replace(tzinfo=None) == time(15),
                    "M2_SUBMIT_CLOCK", "Execution projection must be from this day's 15:00 event")
            require((decision.signal.action != "no_change" or outcome.no_order_reason == "no_change")
                    and (outcome.no_order_reason != "no_change" or decision.signal.action == "no_change"),
                    "M2_OUTCOME_LINK", "No-change signal cannot create native orders")
            if outcome.no_order_reason == "already_at_target":
                require(decision.signal.target_shares == decision.account.quantity,
                        "M2_OUTCOME_LINK", "No-order target differs from the known held quantity")
            for order in outcome.orders:
                delta = decision.signal.target_shares - decision.account.quantity
                require(order.requested_quantity == abs(delta) and order.side == ("buy" if delta > 0 else "sell"),
                        "M2_OUTCOME_LINK", "Native order differs from the committed target intent")
                require(order.order_id not in seen_orders and not set(order.trade_ids) & seen_trades,
                        "M2_DUPLICATE_NATIVE_ID", "Native IDs cannot be counted twice")
                seen_orders.add(order.order_id)
                seen_trades.update(order.trade_ids)
            records.append(outcome)
        if i < len(state.day_closes):
            close = state.day_closes[i]
            require(close.decision_id == decision.decision_id, "M2_OUTCOME_LINK", "Day close refers to another decision")
            validate_account(close.account, session)
            require(close.account.source_kind == "previous_day_committed"
                    and close.account.commit_sequence == close.commit_sequence
                    and close.account.account_as_of.astimezone(SHANGHAI).date() == days[i].trading_date
                    and close.account.account_as_of.astimezone(SHANGHAI).time() >= time(15),
                    "M2_ACCOUNT_SOURCE_MISMATCH", "Day close must preserve that day's native committed account source")
            require(len(close.final_orders) == len(outcome.orders), "M2_OUTCOME_LINK", "Day close order set differs")
            for old, final in zip(outcome.orders, close.final_orders, strict=True):
                require(final.order_id == old.order_id and final.requested_quantity == old.requested_quantity
                        and final.side == old.side and final.security == old.security
                        and final.filled_quantity == old.filled_quantity and final.trade_ids == old.trade_ids
                        and final.status in {"filled", "cancelled", "rejected"},
                        "M2_DAY_CLOSE_ORDER", "EOD preserves fills and records native terminal status; no new matching")
                require(old.status not in {"filled", "rejected", "cancelled"} or final.status == old.status,
                        "M2_DAY_CLOSE_ORDER", "A terminal native order cannot change terminal status")
            records.append(close)
            previous_account = close.account
        for record in records:
            require(record.commit_sequence > prior_seq and record.cursor_after > prior_cursor,
                    "M2_COMMIT_ORDER", "Commit sequence and native event cursor must strictly advance")
            if isinstance(record, DecisionRecord):
                require(record.cursor_after > record.context.event_cursor, "M2_COMMIT_ORDER", "Decision must commit after its input event")
            prior_seq, prior_cursor = record.commit_sequence, record.cursor_after
    require(not state.complete or len(state.day_closes) == len(days), "M2_INCOMPLETE_WINDOW", "Incomplete window cannot be terminal")


def checkpoint_extension(state):
    validate_state(state)
    body = state.model_dump(mode="json")
    return {"m2_state": body, "m2_sha256": digest(body)}


def restore_extension(envelope, *, expected_session_sha256):
    # Called only after the outer native checkpoint's identity/integrity check.
    try:
        require(type(envelope) is dict and set(envelope) == {"m2_state", "m2_sha256"},
                "M2_CHECKPOINT_INVALID", "Unexpected M2 checkpoint fields")
        require(digest(envelope["m2_state"]) == envelope["m2_sha256"], "M2_CHECKPOINT_INVALID", "M2 checkpoint bytes differ")
        state = Journal.model_validate_json(json.dumps(envelope["m2_state"]))
        require(state.session.identity == expected_session_sha256, "M2_CHECKPOINT_IDENTITY", "Use the original run/request/strategy/data/profile")
        validate_state(state)
        return state
    except (ValidationError, KeyError, TypeError, ValueError):
        raise DomainError("M2_CHECKPOINT_INVALID", "Invalid M2 checkpoint extension", 409) from None
