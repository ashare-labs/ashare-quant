"""Isolated native-host wiring. RQ is the sole book; this module records projections."""

import json
from decimal import Decimal
from pathlib import Path

from rqalpha.const import INSTRUMENT_TYPE, ORDER_STATUS, POSITION_DIRECTION
from rqalpha.core.events import EVENT
from rqalpha.environment import Environment
from rqalpha.interface import AbstractFrontendValidator, AbstractMod

from ashare_quant.br1_fixed.state import CompletedReceipt, session
from ashare_quant.contracts import SHANGHAI, DomainError
from ashare_quant.d1 import PreviewRequest, plan
from ashare_quant.d1_boundary import fixed_records
from ashare_quant.d1_bounded_adapter import BoundedHost, ListingCheckpoint
from ashare_quant.d1_listing import ListingOwner
from ashare_quant.d1_strategy_boundary import (
    NativeOutcome,
    check_native_pair,
    select_predefined_intent,
)
from ashare_quant.rq_mod import atomic_json, money
from rqprobe.checkpoint import kill_at
from rqprobe.events import RecordedEventSource
from rqprobe.guards import block_rqdata_apis

__config__ = {"priority": 1000}


class Trap:
    def __getattribute__(self, name):
        raise RuntimeError("BR1_SELECTOR_NATIVE_INPUT_FORBIDDEN:" + name)
    def __bool__(self):
        raise RuntimeError("BR1_SELECTOR_NATIVE_INPUT_FORBIDDEN:truth")


class PlannedValidator(AbstractFrontendValidator):
    def __init__(self, mod):
        self.mod = mod

    def validate_submission(self, order, account=None):
        m = self.mod
        i = next((i for i in plan().intents if i.at.replace(tzinfo=None) == m.env.calendar_dt), None)
        if (i is None or (order.order_book_id, order.side.name.lower(), order.quantity) !=
                (i.security, i.side, i.quantity) or not m.submitting):
            raise DomainError("BR1_UNPLANNED_ORDER", "Only the selected fixed intent may enter native broker", 422)
        if Decimal(str(order.frozen_price)) % Decimal("0.01"):
            raise DomainError("BR1_TICK_MISMATCH", "Native order price violates the fixed tick", 422)
        m.audit("planned_native_validator", {"order_id": order.order_id, "lot": order.quantity,
                                              "tick": "0.01", "frozen_price": order.frozen_price})
        return None

    def validate_cancellation(self, order, account=None):
        return None


class FixedCheckpoint(ListingCheckpoint):
    def read(self):
        payload = super().read()
        if payload and payload["complete"]:
            # The boundary checked the entire saved domain before this signal.
            # Do not restore an account or enter the executor for completed work.
            raise CompletedReceipt(payload["receipt"])
        return payload


class FixedBR1Mod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.config = env, config
        self.selecting, self.submitting = False, False
        env.global_vars.br1_state = {"consumed": [], "orders": [], "trades": [], "states": [], "selection": [], "queries": []}
        self.trace_path = Path(config.trace)
        owner = ListingOwner(config.store)
        self.binding = owner.snapshot.report.model_dump(mode="json")
        env.set_event_source(RecordedEventSource(env, fixed_records(owner.contract)))
        self.host = BoundedHost(env, owner, run_identity=config.run_identity)
        session["host"] = self.host
        self.host.bind_data(defer_events=True)
        session["proxy_factory"] = self.host.native_proxy_factory()
        session["proxy_factory"].__enter__()
        self.ds = self.host.source
        env.set_persist_provider(FixedCheckpoint(config.checkpoint, self.host.boundary.identity,
                                                 self.host.boundary))
        session["identity"] = self.host.boundary.identity
        session["effective_config"] = self.host.boundary._config
        session["runtime_epoch"] = self.host.boundary.life.epoch
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.install_phase_events)
        self.blocked = block_rqdata_apis()
        env.add_frontend_validator(PlannedValidator(self), INSTRUMENT_TYPE.CS)
        for event in (EVENT.POST_BEFORE_TRADING, EVENT.POST_BAR, EVENT.POST_AFTER_TRADING, EVENT.POST_SETTLEMENT):
            env.event_bus.add_listener(event, self.observe)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        for event in (EVENT.ORDER_PENDING_NEW, EVENT.ORDER_CREATION_PASS,
                      EVENT.ORDER_CREATION_REJECT, EVENT.ORDER_UNSOLICITED_UPDATE):
            env.event_bus.add_listener(event, self.order_event)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)

    @property
    def state(self):
        return self.env.global_vars.br1_state

    def install_phase_events(self, event):
        # Native Portfolio installs its PRE callbacks during INIT_PORTFOLIO.
        # Own the first slot after all fixed system initialization has finished.
        self.host.bind_phase_events()

    def get_state(self):
        return self.host.get_state()

    def set_state(self, state):
        self.host.set_state(state)

    def audit(self, kind, details, *, committed=True):
        if self.selecting:
            raise RuntimeError("BR1_SELECTOR_EXECUTION_DATA_ACCESS:" + kind)
        row = {"invocation": session["invocation"], "kind": kind, "model_clock": str(self.env.calendar_dt), "details": details}
        row = json.loads(json.dumps(row, default=str))
        with self.trace_path.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if committed:
            self.state["queries"].append(row)

    def account(self):
        p = self.env.portfolio.get_position("600000.XSHG", POSITION_DIRECTION.LONG)
        return {"cash": money(self.env.portfolio.cash), "quantity": p.quantity, "closable": p.closable,
                "average": money(p.avg_price), "receivable": money(p.dividend_receivable),
                "equity": money(self.env.portfolio.total_value), "cost": money(p.transaction_cost)}

    def observe(self, event):
        row = {"event": event.event_type.name, "model_clock": str(self.env.calendar_dt), **self.account()}
        self.state["states"].append(row)
        self.audit("native_account_projection", row)
        if event.event_type == EVENT.POST_BAR:
            count = 1 if self.env.calendar_dt.date().isoformat() == "2020-01-03" else 2
            if len(self.state["orders"]) != count:
                raise DomainError("BR1_MISSING_PLANNED_ORDER", "Missing or extra native intent", 422)
        kill_at(event.event_type.name.lower(), self.env.event_source.cursor + 1)

    def trade(self, event):
        t = event.trade
        row = {"trade_id": str(t.exec_id), "order_id": str(t.order_id), "model_clock": str(t.trading_datetime),
               "side": t.side.name.lower(), "quantity": t.last_quantity, "price": money(t.last_price),
               "commission": money(t.commission), "tax": money(t.tax)}
        self.state["trades"].append(row)
        self.audit("native_trade", row)

    def order_event(self, event):
        order = getattr(event, "order", None)
        self.audit("native_order_event", {"event": event.event_type.name,
            "order_id": str(order.order_id) if order else None,
            "status": order.status.name if order else None, "reason": getattr(event, "reason", None)})
        if event.event_type == EVENT.ORDER_CREATION_REJECT or (order and order.status in (ORDER_STATUS.REJECTED, ORDER_STATUS.CANCELLED)):
            raise DomainError("BR1_INTENT_NOT_FULLY_FILLED", "Native order rejected or cancelled", 422)

    def dispatch(self):
        request = PreviewRequest(logical_at=self.env.calendar_dt.replace(tzinfo=SHANGHAI), phase="BAR",
                                 consumed_intents=tuple(self.state["consumed"]))
        getter = Environment.__dict__["get_instance"]
        def forbidden(cls):
            raise RuntimeError("BR1_SELECTOR_ENVIRONMENT_FORBIDDEN")
        query_count = len(self.state["queries"])
        self.selecting = True
        Environment.get_instance = classmethod(forbidden)
        try:
            selected = select_predefined_intent(request, native_context=Trap(), bar_dict=Trap(),
                current_snapshot=Trap(), price_board=Trap(), history_bars=Trap())
        finally:
            Environment.get_instance = getter
            self.selecting = False
        if len(self.state["queries"]) != query_count:
            raise RuntimeError("BR1_SELECTOR_DATA_READ")
        self.state["selection"].append({"input": request.model_dump(mode="json"),
            "output": selected.model_dump(mode="json"), "execution_data_reads": 0,
            "native_context_and_market_channels": "TRAPPED", "environment_lookup": "BLOCKED_DURING_SELECTION"})
        i = selected.intent
        if i is None:
            return
        from rqalpha.api import order_shares
        self.submitting = True
        try:
            result = order_shares(i.security, i.quantity if i.side == "buy" else -i.quantity)
        finally:
            self.submitting = False
        if result is None:
            raise DomainError("BR1_INTENT_NOT_FULLY_FILLED", "Native API returned no order", 422)
        if isinstance(result, list):
            if len(result) != 1:
                raise DomainError("BR1_UNPLANNED_ORDER", "Expected one native order", 422)
            result = result[0]
        row = NativeOutcome(order_id=str(result.order_id), security=result.order_book_id,
             submitted_at=result.datetime.replace(tzinfo=SHANGHAI), side=result.side.name.lower(),
             quantity=result.quantity, filled=result.filled_quantity, status="filled" if result.status == ORDER_STATUS.FILLED else "active")
        from ashare_quant.d1_strategy_boundary import check_native_outcome
        check_native_outcome(len(self.state["orders"]), row)
        self.state["orders"].append(row.model_dump(mode="json"))
        self.state["consumed"].append(i.intent_id)
        self.audit("native_order_final", row.model_dump(mode="json"))

    def receipt(self):
        return {"identity": self.host.boundary.identity, "scope": "user_authorized_fixed_BR1_conditional_experiment",
                "evidence_kind": "conditional_research", "general_execution_permission": False,
                "binding": self.binding, "state": self.account(), **self.state,
                "actual_ad08_calls": [x.model_dump(mode="json") for x in self.ds.actual_history_calls],
                "runtime_epoch": self.host.boundary.life.epoch,
                "restore_generation": self.host.boundary.generation,
                "listing_projection": self.host.boundary.projection_metadata(),
                "blocked_external_apis": self.blocked,
                "matching_type": str(self.env.config.mod.sys_simulation.matching_type),
                "native_classes": {k: type(getattr(self.env, k)).__module__ + "." + type(getattr(self.env, k)).__name__
                                   for k in ("broker", "portfolio", "data_source", "price_board", "event_source")}}

    def finish(self, event):
        check_native_pair(tuple(NativeOutcome.model_validate(o) for o in self.state["orders"]))
        self.result = self.receipt()
        self.env.persist_helper.receipt = self.result
        self.env.persist_helper.complete = True
        self.env.persist_helper.persist()

    def tear_down(self, code, exception=None):
        if exception is None and hasattr(self, "result"):
            atomic_json(self.config.result, self.result)
            return self.result


def dispatch_fixed_intent():
    Environment.get_instance().mod_dict["bounded"].dispatch()


def load_mod():
    return FixedBR1Mod()
