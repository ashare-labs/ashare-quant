"""B12 strategy query semantics; native broker, matcher and account stay owners.

The model covers the saved single-day minute observations only. It does not
assert an independent 09:25 match or relabel a native 09:30 execution.
"""

from decimal import Decimal

from .host import Host, StrategyAPI

MODEL = "aq.b12.observed-opening.v1"
CONTROLS = ("keep_0920", "cancel_0928")


def control_plan(case):
    clocks = [("before", "09:00")]
    clocks += [("callback", x) for x in ("09:20", "09:24", "09:25", "09:26", "09:28")]
    clocks += [("opening", "09:30"), ("bar", "09:31"), ("bar", "09:32"),
               ("after", "15:30"), ("end_clock", "23:59:59")]
    return tuple({"index": i, "kind": kind, "at": case.at(at).isoformat()}
                 for i, (kind, at) in enumerate(clocks, 1))


class OpeningAPI(StrategyAPI):
    """Native order handles remain usable even while the open query is empty."""

    @property
    def cancel_at_checkpoint(self):
        return self._host.control == "cancel_0928"

    def get_open_orders(self):
        h = self._host
        return {str(o.order_id): o for o in h.env.broker.get_open_orders()
                if h.strategy_status(o) == "open"}

    def cancel(self, order):
        from rqalpha.api import cancel_order

        h = self._host
        if (h.control != "cancel_0928" or self.clock != "09:28"
                or h.orders.get(str(order.order_id)) is not order
                or h.opening_started or not order.is_active() or order.filled_quantity != 0):
            raise RuntimeError("B12_CANCEL_INTENT_OUT_OF_SCOPE")
        h.emit("cancel_request", order=h.project_order(order))
        answer = cancel_order(order)
        # Record the real native return. Do not synthesize a native Order field.
        h.emit("cancel_return", returned_none=answer is None,
               native_type=type(answer).__module__ + "." + type(answer).__name__,
               returned_order=None if answer is None else h.project_order(answer),
               order=h.project_order(order))
        return answer


class OpeningHost(Host):
    def __init__(self, case, provider, metadata, plan, root, *, control=None):
        super().__init__(case, provider, metadata, plan, root)
        if case.case_id != "B12" or control not in (None, *CONTROLS):
            raise ValueError("B12_OPENING_SCOPE_REQUIRED")
        self.control = control
        self.opening_started = False
        self.cancel_times = {}
        self.api = OpeningAPI(self)

    def initialize(self):
        if self.control is None:
            return super().initialize()
        from .strategies import b12_control

        b12_control.initialize(self.api)
        self.started = True
        self.emit("strategy_initialized", callbacks=list(self.callbacks), control=self.control)

    def dispatch(self):
        super().dispatch()
        # The 09:30 callback observes 'new'; matching begins only after it exits.
        if self.at == self.case.at("09:30"):
            self.opening_started = True

    def strategy_status(self, order):
        native = order.status.name
        if native in {"PENDING_NEW", "ACTIVE"}:
            return "open" if self.opening_started else "new"
        return {"FILLED": "held", "CANCELLED": "canceled", "REJECTED": "rejected"}.get(
            native, native.lower())

    def project_order(self, order):
        value = super().project_order(order)
        fills = [t for t in self.trades if t["order_id"] == str(order.order_id)]
        return {**value, "strategy_status": self.strategy_status(order), "status_model": MODEL,
                "finished_at": fills[-1]["at"] if order.status.name == "FILLED" and fills else None,
                "finished_at_basis": "native_trade.trading_datetime",
                "canceled_at": self.cancel_times.get(str(order.order_id)),
                "canceled_at_basis": "native_cancellation_event_clock"}

    def snapshot(self, label):
        self.emit("snapshot", label=label, account=self.account(),
                  orders=[self.project_order(o) for o in self.orders.values()],
                  open_order_ids=list(self.api.get_open_orders()),
                  native_open_order_ids=[str(o.order_id) for o in self.env.broker.get_open_orders()],
                  order_query_model=MODEL, trades=self.trades)

    def order_event(self, event):
        if event.event_type.name == "ORDER_CANCELLATION_PASS":
            self.cancel_times[str(event.order.order_id)] = self.at.isoformat()
        super().order_event(event)

    def complete(self):
        result = super().complete()
        if self.control == "cancel_0928":
            result["scenario_triggered"] = bool(
                self.submissions == 1 and not self.trades and len(self.orders) == 1
                and result["orders"][0]["native_status"] == "CANCELLED"
                and result["orders"][0]["canceled_at"] == self.case.at("09:28").isoformat()
                and Decimal(result["final_account"]["frozen_cash"]) == 0
                and Decimal(result["final_account"]["cash"]) == Decimal(self.case.initial_cash)
                and any(r["kind"] == "cancel_return" for r in self.records))
            result["status"] = "SCENARIO_EXECUTED" if result["scenario_triggered"] else "SCENARIO_NOT_TRIGGERED"
        result.update(opening_query_model=MODEL, b12_control=self.control)
        return result
