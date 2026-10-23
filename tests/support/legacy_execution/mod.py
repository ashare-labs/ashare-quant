"""One native book, with atomic strategy intent and read-only execution projections."""
import json
from datetime import datetime, time
from pathlib import Path

from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION
from rqalpha.core.events import EVENT
from rqalpha.environment import Environment
from rqalpha.interface import AbstractFrontendValidator, AbstractMod

from ashare_quant.contracts import SHANGHAI
from ashare_quant.execution.rqalpha.checkpoint import AtomicFile, StrictHelper, kill_at
from ashare_quant.execution.rqalpha.events import RecordedEventSource
from ashare_quant.execution.rqalpha.guards import block_rqdata_apis
from ashare_quant.execution.serialization import money
from ashare_quant.storage.canonical import digest, require
from tests.support.legacy_backtests.contracts import AccountInput, DecisionContext
from tests.support.legacy_backtests.journal import (
    DayClose,
    ExecutionOutcome,
    NativeOrderProjection,
    account_digest,
    checkpoint_extension,
    initial_state,
    pending_for_submit,
    prepare_complete,
    prepare_day_close,
    prepare_decision,
    prepare_execution,
    restore_extension,
)
from tests.support.legacy_execution.adapter import SID, OwnerBoard, OwnerSource, actual_previous
from tests.support.legacy_execution.state import runtime
from tests.support.legacy_provider.owner_wire import pack

__config__ = {"priority": 1000}


class CheckedCheckpoint(AtomicFile):
    def read(self):
        payload = super().read()
        if payload:
            state = json.loads(payload["states"]["mod_m2native"])
            journal = restore_extension(state["journal"], expected_session_sha256=runtime["client"].session.identity)
            cursor = json.loads(payload["states"]["event_source"])["cursor"]
            clock = datetime.fromisoformat(state["at"]).replace(tzinfo=None)
            require(tuple(payload["extras"]["clock"]) == (clock, clock), "M2_ATOMIC_CLOCK", "Native and adapter clock differ")
            require(cursor == journal.last_cursor and state["cursor"] == cursor,
                    "M2_ATOMIC_CURSOR", "Native cursor and journal do not share the same commit")
            require(not payload["complete"] or journal.complete, "M2_ATOMIC_COMPLETE", "Complete receipt requires complete journal")
        return payload


class M2Helper(StrictHelper):
    def restore(self, event=None):
        keys = ("event_source", "mod_m2native", "universe")
        require(all(k in self.objects for k in keys), "M2_NATIVE_REGISTRY", "Native restore registry differs")
        self.objects = {**{k: self.objects[k] for k in keys}, **self.objects}
        return super().restore(event)


class IntentValidator(AbstractFrontendValidator):
    def __init__(self, host):
        self.host = host

    def validate_submission(self, order, account=None):
        h = self.host
        require(h.submitting and h.phase == "SUBMIT_MATCH", "M2_UNPLANNED_ORDER", "Only pending committed intent may submit")
        decision = pending_for_submit(h.journal, h.at)
        delta = decision.signal.target_shares - decision.account.quantity
        require((order.order_book_id, order.quantity, order.side.name.lower()) ==
                (SID, abs(delta), "buy" if delta > 0 else "sell"), "M2_UNPLANNED_ORDER", "Order differs from committed intent")

    def validate_cancellation(self, order, account=None):
        return None


class NativeM2Mod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.config, self.client = env, config, runtime["client"]
        runtime["mod"] = self
        self.days = self.client.session.days
        self.scope = self.days[0]
        self.at = datetime.combine(self.scope.trading_date, time(8, 30), SHANGHAI)
        self.phase, self.closed, self.submitting = "INITIALIZE", False, False
        self.generation = runtime["generation"]
        self.epoch = f"owner-{self.generation}"
        self.journal = initial_state(self.client.session)
        self.previous, self.bridge = None, None
        self.reads, self.observations, self.trades, self.orders, self.accounts = [], [], [], {}, []
        self.state_reads, self.query_cache = {}, {}
        self.trace = Path(config.trace)
        env.set_event_source(RecordedEventSource(env, runtime["records"]))
        self.source = OwnerSource(self)
        env.set_data_source(self.source)
        env.set_price_board(OwnerBoard(self))
        env.set_persist_provider(CheckedCheckpoint(config.checkpoint, runtime["identity"]))
        env.add_frontend_validator(IntentValidator(self), INSTRUMENT_TYPE.CS)
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.bind_events)
        env.event_bus.add_listener(EVENT.POST_BAR, self.execution_commit)
        env.event_bus.add_listener(EVENT.POST_AFTER_TRADING, self.day_close)
        env.event_bus.add_listener(EVENT.POST_SETTLEMENT, self.settled)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        for e in (EVENT.ORDER_PENDING_NEW, EVENT.ORDER_CREATION_PASS, EVENT.ORDER_CREATION_REJECT,
                  EVENT.ORDER_UNSOLICITED_UPDATE, EVENT.ORDER_CANCELLATION_PASS, EVENT.ORDER_CANCELLATION_REJECT):
            env.event_bus.add_listener(e, self.order_event)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)
        self.blocked = block_rqdata_apis()

    @property
    def current_day(self):
        return self.scope.trading_date

    def alive(self):
        require(not self.closed and Environment._env is self.env, "M2_NATIVE_LIFECYCLE", "Native owner is closed or replaced")

    def context(self):
        s = self.client.session
        return DecisionContext(run_id=s.run_id, request_sha256=digest(s.request), window_id=s.request.window_id,
            trading_date=self.current_day, previous_open=self.scope.previous_open,
            logical_at=datetime.combine(self.current_day, time(9), SHANGHAI), event_cursor=self.env.event_source.cursor,
            generation=self.generation, epoch=self.epoch)

    def record(self, kind, details):
        row = {"kind": kind, "phase": self.phase, "at": self.at.isoformat(),
               "cursor": self.env.event_source.cursor, "generation": self.generation, "details": details}
        with self.trace.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.observations.append(row)

    def engine(self, operation, **kwargs):
        self.alive()
        c = self.client.engine_context(self.context(), phase=self.phase, logical_at=self.at)
        key = digest({"context": c.to_dict(), "operation": operation, "parameters": kwargs})
        if key not in self.query_cache:
            read = self.client.engine_read(operation, c, **kwargs)
            self.query_cache[key] = read.result.to_dict()
            self.reads.append({"context": c.to_dict(), "documents": pack(read).model_dump(mode="json")})
            self.record("owner_engine_read", {"operation": operation, "evidence_sha256": read.evidence.sha256,
                                               "authorization_sha256": read.authorization.sha256})
            if operation == "trading_state":
                self.state_reads[self.current_day.isoformat()] = read.result.to_dict()
        return self.query_cache[key]

    def last_state(self):
        require(self.current_day.isoformat() in self.state_reads, "M2_STATE_NOT_OBSERVED", "No current T15 state observation")
        return self.state_reads[self.current_day.isoformat()]

    def listing(self, target):
        if target == self.current_day:
            role = "current"
        elif self.phase == "INITIALIZE" and target == self.scope.previous_open:
            role = "warmup"
        elif self.phase == "DECIDE" and target == self.scope.previous_open:
            role = "previous"
        elif self.phase == "SETTLEMENT" and target == self.scope.next_open:
            role = "settlement_successor"
        else:
            raise RuntimeError("M2_LISTING_ROLE_OUTSIDE_CURRENT_PHASE")
        return self.engine("listing", target_date=target, role=role)

    def bind_events(self, event):
        for e in (EVENT.PRE_BEFORE_TRADING, EVENT.PRE_BAR, EVENT.PRE_AFTER_TRADING, EVENT.PRE_SETTLEMENT):
            self.env.event_bus.prepend_listener(e, self.phase_event)

    def phase_event(self, event):
        self.alive()
        self.at = self.env.calendar_dt.replace(tzinfo=SHANGHAI)
        self.scope = next(d for d in self.days if d.trading_date == self.at.date())
        self.phase = {EVENT.PRE_BEFORE_TRADING: "DECIDE", EVENT.PRE_BAR: "SUBMIT_MATCH",
                      EVENT.PRE_AFTER_TRADING: "AFTER_TRADING", EVENT.PRE_SETTLEMENT: "SETTLEMENT"}[event.event_type]
        self.query_cache.clear()
        if self.phase == "DECIDE":
            self.previous = None
            # Observe P before native account housekeeping can request its P valuation.
            # This is a declared read for the fixed band template, not a signal evaluation.
            actual_previous(self)

    def decide(self):
        require(self.phase == "DECIDE", "M2_DECIDE_CLOCK", "Decision only at T09")
        read = actual_previous(self)  # Actual RQ cache hit, refreshed owner authorization.
        account = self.journal.day_closes[-1].account if self.journal.day_closes else self.journal.initial_account
        getter = Environment.__dict__["get_instance"]
        def deny(cls):
            raise RuntimeError("M2_PURE_STRATEGY_NATIVE_ACCESS")
        Environment.get_instance = classmethod(deny)
        try:
            from tests.support.legacy_python.spec import is_python
            python_receipt = None
            if is_python(self.client.session.strategy):
                from tests.support.legacy_python.runtime import invoke
                python_receipt = invoke(self.client.session, self.context(), account, read, runtime["root"], runtime["invocation"])
            self.journal = prepare_decision(self.journal, self.context(), account, read,
                commit_sequence=self.journal.last_sequence + 1, cursor_after=self.env.event_source.cursor + 1,
                python_receipt=python_receipt)
        finally:
            Environment.get_instance = getter
        self.record("decision_candidate", {"decision_id": self.journal.decisions[-1].decision_id,
            "signal": self.journal.decisions[-1].signal.model_dump(mode="json"),
            "read_evidence_sha256": digest(read), "execution_information_supplied_to_strategy": False})
        kill_at("decision_candidate", self.env.event_source.cursor + 1)

    def submit(self):
        decision = pending_for_submit(self.journal, self.at)
        if decision.signal.action == "no_change":
            return
        delta = decision.signal.target_shares - decision.account.quantity
        if not delta:
            return
        from rqalpha.api import order_shares
        self.submitting = True
        try:
            result = order_shares(SID, delta)
        finally:
            self.submitting = False
        if result is not None:
            if isinstance(result, list):
                require(len(result) == 1, "M2_NATIVE_ORDER_COUNT", "At most one order per intent")
                result = result[0]
            self.project_order(result)
        self.record("native_submit_return", {"decision_id": decision.decision_id, "order_id": str(result.order_id) if result is not None else None})
        kill_at("native_submit", self.env.event_source.cursor + 1)

    def project_order(self, order):
        status = order.status.name.lower()
        if status in {"pending_new", "pending_cancel"}:
            status = "active"
        if status == "active" and order.filled_quantity:
            status = "partially_filled"
        row = NativeOrderProjection(order_id=str(order.order_id), side=order.side.name.lower(),
            requested_quantity=int(order.quantity), filled_quantity=int(order.filled_quantity),
            remaining_quantity=int(order.unfilled_quantity), status=status,
            trade_ids=tuple(t["trade_id"] for t in self.trades if t["order_id"] == str(order.order_id)),
            reason=order.message or None)
        self.orders[row.order_id] = row.model_dump(mode="json")
        return row

    def order_event(self, event):
        order = getattr(event, "order", None)
        self.record("native_order_event", {"event": event.event_type.name, "reason": getattr(event, "reason", None),
            "order_id": str(order.order_id) if order else None})
        if order is not None:
            # TRADE callbacks may run before/after native order's fill mutation. Final
            # projection is taken after POST_BAR; early observations retain raw status.
            if not order.filled_quantity or any(t["order_id"] == str(order.order_id) for t in self.trades):
                self.project_order(order)

    def trade(self, event):
        t = event.trade
        self.trades.append({"trade_id": str(t.exec_id), "order_id": str(t.order_id),
            "at": t.trading_datetime.replace(tzinfo=SHANGHAI).isoformat(), "side": t.side.name.lower(),
            "quantity": int(t.last_quantity), "price": money(t.last_price), "commission": money(t.commission), "tax": money(t.tax)})
        self.record("native_trade", self.trades[-1])

    def account(self):
        p = self.env.portfolio.get_position(SID, POSITION_DIRECTION.LONG)
        return {"cash": money(self.env.portfolio.cash), "quantity": int(p.quantity), "sellable": int(p.closable),
                "average": money(p.avg_price), "receivable": money(p.dividend_receivable),
                "equity": money(self.env.portfolio.total_value), "cost": money(p.transaction_cost)}

    def day_orders(self):
        used = {o.order_id for x in self.journal.executions for o in x.orders}
        return tuple(NativeOrderProjection.model_validate(v) for k, v in self.orders.items() if k not in used)

    def execution_commit(self, event):
        decision = pending_for_submit(self.journal, self.at)
        orders = self.day_orders()
        reason = None
        if not orders:
            reason = "no_change" if decision.signal.action == "no_change" else (
                "already_at_target" if decision.signal.target_shares == decision.account.quantity else "native_rejected_without_order")
        account = self.account()
        native = {"orders": orders, "trades": self.trades, "account": account}
        self.journal = prepare_execution(self.journal, ExecutionOutcome(decision_id=decision.decision_id,
            commit_sequence=self.journal.last_sequence + 1, cursor_after=self.env.event_source.cursor + 1,
            execution_at=self.at, native_receipt_sha256=digest(native), native_account_sha256=digest(account),
            orders=orders, no_order_reason=reason, native_reason="native API returned no order; inspect native event trace" if reason == "native_rejected_without_order" else None))
        self.accounts.append({"phase": self.phase, "at": self.at.isoformat(), **account})
        kill_at("execution_candidate", self.env.event_source.cursor + 1)

    def day_close(self, event):
        account = self.account()
        seq = self.journal.last_sequence + 1
        body = dict(run_id=self.client.session.run_id, account_id="bt-" + self.client.session.run_id,
            source_kind="previous_day_committed", commit_sequence=seq, account_as_of=self.at,
            cash=account["cash"], quantity=account["quantity"], sellable=account["sellable"])
        projection = AccountInput(**body, snapshot_sha256="0" * 64)
        projection = projection.model_copy(update={"snapshot_sha256": account_digest(projection)})
        final = tuple(NativeOrderProjection.model_validate(self.orders[o.order_id]) for o in self.journal.executions[-1].orders)
        self.journal = prepare_day_close(self.journal, DayClose(decision_id=self.journal.decisions[-1].decision_id,
            commit_sequence=seq, cursor_after=self.env.event_source.cursor + 1,
            native_receipt_sha256=digest({"account": account, "orders": final}), account=projection, final_orders=final))
        self.accounts.append({"phase": self.phase, "at": self.at.isoformat(), **account})

    def settled(self, event):
        self.record("native_settlement", self.account())

    def get_state(self):
        return json.dumps({"journal": checkpoint_extension(self.journal), "phase": self.phase, "at": self.at.isoformat(),
            "cursor": self.env.event_source.cursor, "reads": self.reads, "observations": self.observations,
            "trades": self.trades, "orders": self.orders, "accounts": self.accounts,
            "state_reads": self.state_reads}, sort_keys=True).encode()

    def set_state(self, raw):
        state = json.loads(raw)
        self.journal = restore_extension(state["journal"], expected_session_sha256=self.client.session.identity)
        self.phase, self.at = state["phase"], datetime.fromisoformat(state["at"])
        self.scope = next(d for d in self.days if d.trading_date == self.at.date())
        for key in ("reads", "observations", "trades", "orders", "accounts", "state_reads"):
            setattr(self, key, state[key])
        self.previous = self.journal.decisions[-1].read_evidence if self.phase == "DECIDE" and self.journal.decisions else None
        self.query_cache.clear()
        self.record("restored_native_state", {"journal_cursor": self.journal.last_cursor, "pending_submit": len(self.journal.decisions) > len(self.journal.executions)})

    def finish(self, event):
        self.journal = prepare_complete(self.journal)
        self.result = {"identity": runtime["identity"], "session_identity": self.client.session.identity,
            "scope": "user_authorized_bounded_m2_conditional_research", "general_execution_permission": False,
            "native_AD08_observed": True, "historical_pit_verified": False, "paper_admitted": False,
            "journal": checkpoint_extension(self.journal), "owner_reads": self.reads, "native_observations": self.observations,
            "native_trades": self.trades, "native_orders": self.orders, "native_accounts": self.accounts,
            "final_native_account": self.account(), "generation": self.generation,
            "native_classes": {k: type(getattr(self.env, k)).__module__ + "." + type(getattr(self.env, k)).__name__
                for k in ("broker", "portfolio", "data_proxy", "data_source", "event_source")},
            "instrument_dates": "absolute listing date absent; delisting marker only for native universe cleanup; no absolute date claimed"}
        self.env.persist_helper.complete, self.env.persist_helper.receipt = True, self.result
        self.env.persist_helper.persist()

    def tear_down(self, code, exception=None):
        self.closed = True
        return getattr(self, "result", None)


def load_mod():
    return NativeM2Mod()
