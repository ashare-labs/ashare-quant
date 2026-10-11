"""Trusted callback facade and read-only native projections for six scenarios."""

import importlib
import json
from datetime import datetime
from decimal import Decimal

from rqalpha.const import EXECUTION_PHASE, INSTRUMENT_TYPE
from rqalpha.core.events import EVENT, Event
from rqalpha.core.execution_context import ExecutionContext
from rqalpha.interface import AbstractMod

from ashare_quant.contracts import SHANGHAI

from .contracts import number
from .native import Board, Events, Matcher, Source
from .state import runtime


class StrategyAPI:
    def __init__(self, host):
        self._host = host

    @property
    def clock(self):
        return self._host.at.strftime("%H:%M")

    @property
    def available_cash(self):
        return Decimal(number(self._host.env.portfolio.stock_account.cash))

    def schedule(self, clock, callback):
        h = self._host
        clocks = {datetime.fromisoformat(r["at"]).strftime("%H:%M") for r in h.plan}
        if h.started or clock not in clocks or clock in h.callbacks:
            raise RuntimeError("SIX_CALLBACK_OUT_OF_SCOPE")
        h.callbacks[clock] = callback

    def emit(self, kind, **payload):
        self._host.emit(kind, **payload)

    def snapshot(self, label):
        self._host.snapshot(label)

    def observe_market(self, label):
        quote = self._host.read("quote", consumer="strategy")
        history = self._host.read("history", consumer="strategy", count=4)
        self.emit("market_observation", label=label, quote=quote, history=history)
        return quote

    def observe_auction(self):
        # An unavailable public result must be explicit; other errors stop the run.
        value = self._host.read("auction", consumer="strategy")
        self.emit("auction_observation", result=value)

    def buy(self, amount, *, limit=None):
        from rqalpha.api import order_shares
        from rqalpha.model.order import LimitOrder, MarketOrder
        h = self._host
        if h.submissions or amount != h.case.quantity or self.clock != h.case.submit_at:
            raise RuntimeError("SIX_ORDER_INTENT_OUT_OF_SCOPE")
        style_name = "limit" if limit is not None else "market"
        if style_name != h.case.style or (h.case.case_id == "B12" and limit != Decimal("11.00")):
            raise RuntimeError("SIX_ORDER_STYLE_OUT_OF_SCOPE")
        h.submissions += 1
        h.emit("order_request", quantity=amount, style="limit" if limit is not None else "market",
               limit_price=str(limit) if limit is not None else None)
        style = LimitOrder(float(limit)) if limit is not None else MarketOrder()
        order = order_shares(h.case.security, amount, price_or_style=style)
        if order is not None:
            h.orders[str(order.order_id)] = order
        h.emit("order_return", returned_none=order is None,
               order=None if order is None else h.project_order(order))
        return order

    def cancel(self, order):
        from rqalpha.api import cancel_order
        h = self._host
        if h.case.case_id != "B10" or not order.is_active() or not 0 < order.filled_quantity < order.quantity:
            raise RuntimeError("SIX_CANCEL_INTENT_OUT_OF_SCOPE")
        h.emit("cancel_request", order=h.project_order(order))
        answer = cancel_order(order)
        h.emit("cancel_return", returned_none=answer is None,
               native_type=type(answer).__module__ + "." + type(answer).__name__,
               returned_order=None if answer is None else h.project_order(answer),
               order=h.project_order(order))


class Host:
    def __init__(self, case, provider, metadata, plan, root):
        self.case, self.provider, self.metadata, self.plan, self.root = case, provider, metadata, plan, root
        self.at = case.at("00:00")
        self.callbacks, self.orders, self.trades, self.records, self.reads = {}, {}, [], [], []
        self.started, self.opening_pulse, self.submissions, self.closed = False, False, 0, False
        self.api = StrategyAPI(self)

    def security(self, sid):
        if self.closed or sid != self.case.security:
            raise RuntimeError("SIX_SECURITY_OUT_OF_SCOPE_OR_CLOSED")

    def emit(self, kind, **payload):
        value = {"seq": len(self.records) + 1, "case_id": self.case.case_id,
                 "at": self.at.isoformat(), "kind": kind, **payload}
        # Freeze values immediately; later native mutation cannot alter old evidence.
        value = json.loads(json.dumps(value, allow_nan=False))
        self.records.append(value)
        with (self.root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self, operation, consumer="engine", **parameters):
        if self.closed or (consumer == "strategy" and operation not in {"quote", "history", "auction"}):
            raise RuntimeError("SIX_READ_ROLE_DENIED")
        read = self.provider.read(operation, self.at, consumer=consumer, **parameters)
        if read.operation != operation or read.logical_at != self.at:
            raise RuntimeError("SIX_OWNER_RESPONSE_CONTEXT_MISMATCH")
        self.reads.append({"consumer": consumer, "identity": read.identity, **read.to_dict()})
        self.emit("owner_read", operation=operation, consumer=consumer, evidence_sha256=read.identity)
        return read.value

    def initialize(self):
        module = importlib.import_module("ashare_quant.execution_six.strategies." + self.case.case_id.lower())
        module.initialize(self.api)
        self.started = True
        self.emit("strategy_initialized", callbacks=list(self.callbacks))

    def dispatch(self):
        callback = self.callbacks.get(self.api.clock)
        if callback is not None:
            self.emit("callback_enter", callback=callback.__name__)
            with ExecutionContext(EXECUTION_PHASE.SCHEDULED):
                callback(self.api)
            self.emit("callback_exit", callback=callback.__name__)

    def user_event(self, event):
        if event.aq_kind == "end_clock":
            return
        self.dispatch()
        if event.aq_kind == "opening":
            self.emit("opening_match_begin", model="opening_quote_after_observation_callback",
                      independent_auction_matching=False)
            self.opening_pulse = True
            try:
                # Native broker/matcher own fills. No fabricated 09:30 OHLCV bar.
                self.env.broker.on_bar(Event(EVENT.BAR))
            finally:
                self.opening_pulse = False
            self.emit("opening_match_end")

    def before(self):
        self.snapshot("initial")

    def mark_to_close(self, event):
        self.emit("valuation_refresh", source="owner.valuation", native_hook="Account._on_bar",
                  matching=False)
        # A valuation-only native refresh, not a fabricated BAR/matching event.
        self.env.portfolio.stock_account._on_bar(event)

    def after(self):
        self.snapshot("end_of_day")
        self.emit("daily_valuation", value=self.read("valuation"))

    def project_order(self, order):
        return {"order_id": str(order.order_id), "security": order.order_book_id,
                "submitted_at": order.datetime.replace(tzinfo=SHANGHAI).isoformat(),
                "native_status": order.status.name, "style": order.type.name,
                "requested": int(order.quantity), "filled": int(order.filled_quantity),
                "remaining": int(order.unfilled_quantity), "limit_price": number(order.price),
                "average_price": number(order.avg_price), "frozen_price": number(order.frozen_price),
                "transaction_cost": number(order.transaction_cost), "reason": order.message}

    def account(self):
        account = self.env.portfolio.stock_account
        positions = [{"security": p.order_book_id, "quantity": int(p.quantity),
                      "sellable": int(p.closable), "average_price": number(p.avg_price),
                      "last_price": number(p.last_price), "market_value": number(p.market_value)}
                     for p in account.get_positions() if p.quantity]
        return {"cash": number(account.total_cash), "available_cash": number(account.cash),
                "frozen_cash": number(account.frozen_cash), "total_value": number(account.total_value),
                "positions_value": number(account.market_value), "positions": positions}

    def snapshot(self, label):
        self.emit("snapshot", label=label, account=self.account(),
                  orders=[self.project_order(o) for o in self.orders.values()],
                  open_order_ids=[str(o.order_id) for o in self.env.broker.get_open_orders()],
                  trades=self.trades)

    def order_event(self, event):
        order = getattr(event, "order", None)
        if order is not None:
            self.orders[str(order.order_id)] = order
        self.emit("native_order_event", event=event.event_type.name,
                  reason=getattr(event, "reason", None),
                  order=None if order is None else self.project_order(order))

    def trade_event(self, event):
        trade = event.trade
        value = {"trade_id": str(trade.exec_id), "order_id": str(trade.order_id),
                 "security": trade.order_book_id,
                 "at": trade.trading_datetime.replace(tzinfo=SHANGHAI).isoformat(),
                 "quantity": int(trade.last_quantity), "price": number(trade.last_price),
                 "commission": number(trade.commission), "tax": number(trade.tax)}
        self.trades.append(value)
        self.emit("native_trade", trade=value)

    def complete(self):
        self.snapshot("strategy_end")
        self.emit("strategy_end")
        snapshots = [r for r in self.records if r["kind"] == "snapshot"]
        orders = [self.project_order(o) for o in self.orders.values()]
        filled = sum(t["quantity"] for t in self.trades)
        no_skip = not any(r["kind"] == "not_triggered" for r in self.records)
        fulfilled = self.submissions == 1 and no_skip
        if self.case.case_id == "B11":
            fulfilled = fulfilled and filled == 0 and any(
                r["kind"] == "native_order_event" and r["event"] == "ORDER_CREATION_REJECT"
                for r in self.records)
        elif self.case.case_id in {"B10", "B13"}:
            fulfilled = fulfilled and 0 < filled < self.case.quantity and orders[-1]["native_status"] == "CANCELLED"
            if self.case.case_id == "B10":
                fulfilled = fulfilled and any(r["kind"] == "cancel_return" for r in self.records)
        else:
            fulfilled = fulfilled and filled == self.case.quantity
            if self.case.case_id == "B08":
                fulfilled = fulfilled and Decimal(self.trades[0]["price"]) < Decimal(orders[0]["limit_price"])
        return {"case_id": self.case.case_id, "scenario_triggered": bool(fulfilled),
                "status": "SCENARIO_EXECUTED" if fulfilled else "SCENARIO_NOT_TRIGGERED",
                "submissions": self.submissions, "orders": orders, "trades": self.trades,
                "snapshots": snapshots, "final_account": self.account(),
                "owner_reads": self.reads, "event_count": len(self.records),
                "independent_auction_matching": False,
                "cross_platform_comparison": "NOT_PERFORMED",
                "native_classes": {k: type(getattr(self.env, k)).__module__ + "." + type(getattr(self.env, k)).__name__
                                   for k in ("broker", "portfolio", "data_proxy", "data_source", "event_source")}}


class Mod(AbstractMod):
    source_type = Source

    def start_up(self, env, config):
        from rqprobe.guards import block_rqdata_apis
        h = self.host = runtime["host"]
        h.env = env
        env.set_data_source(self.source_type(h))
        env.set_price_board(Board(h))
        env.set_event_source(Events(h))
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.ready)
        env.event_bus.add_listener(EVENT.USER, h.user_event)
        env.event_bus.add_listener(EVENT.PRE_AFTER_TRADING, h.mark_to_close)
        env.event_bus.add_listener(EVENT.TRADE, h.trade_event)
        for name in ("ORDER_PENDING_NEW", "ORDER_CREATION_PASS", "ORDER_CREATION_REJECT",
                     "ORDER_PENDING_CANCEL", "ORDER_CANCELLATION_PASS", "ORDER_CANCELLATION_REJECT",
                     "ORDER_UNSOLICITED_UPDATE"):
            env.event_bus.add_listener(EVENT[name], h.order_event)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)
        block_rqdata_apis()
        model = getattr(config, "reservation_model", None)
        if model is not None:
            from ashare_quant.round_one.reservation_account import install_reservation_account
            install_reservation_account(env, model)

    def ready(self, event):
        h = self.host
        h.env.broker.register_matcher(INSTRUMENT_TYPE.CS, Matcher(h, h.env.config.mod.sys_simulation))

    def finish(self, event):
        self.result = self.host.complete()

    def tear_down(self, code, exception=None):
        return getattr(self, "result", None)


def load_mod():
    return Mod()
