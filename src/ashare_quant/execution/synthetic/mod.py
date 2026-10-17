"""Native-state projections and atomic publication; no cash/position arithmetic."""

import json
import os
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from rqalpha.const import POSITION_DIRECTION
from rqalpha.core.events import EVENT

from ashare_quant.execution.rqalpha.adapter import DataSource, PriceBoard
from ashare_quant.execution.rqalpha.checkpoint import AtomicFile
from ashare_quant.execution.rqalpha.fixture import SID
from ashare_quant.execution.rqalpha.mod import ProbeMod
from ashare_quant.execution.synthetic.fixture import ScenarioProvider
from ashare_quant.native_control import check_control


def money(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("NONFINITE_NATIVE_MONEY")
    # RQAlpha uses float internally. Wire normalization is declared, not source decimal recovery.
    with localcontext() as context:
        context.prec = 64
        return format(number.quantize(Decimal("0.00000001")), "f").rstrip("0").rstrip(".") or "0"


def integer(value):
    if int(value) != value:
        raise ValueError("FRACTIONAL_NATIVE_SHARES_UNSUPPORTED")
    return int(value)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".pending")
    with temp.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PublishingCheckpoint(AtomicFile):
    def __init__(self, path, identity, mod, publication):
        super().__init__(path, identity)
        self.mod, self.publication = mod, publication

    def commit(self, payload, cursor):
        check_control()
        # Projection is committed with native state, input cursor, and completion receipt.
        publication = {"identity": self.identity, "cursor": cursor,
                       "complete": payload["complete"], "receipt": self.mod.receipt(),
                       "checkpoint_present": True,
                       "invocation_id": self.mod.config.invocation_id}
        payload["host_publication"] = publication
        super().commit(payload, cursor)
        # This JSON file is a disposable, atomic view of the native checkpoint above.
        atomic_json(self.publication, publication)


class QuantMod(ProbeMod):
    def start_up(self, env, config):
        super().start_up(env, config)
        self.provider = ScenarioProvider(config.scenario)
        ds = DataSource(env, self.provider)
        env.set_data_source(ds)
        env.set_price_board(PriceBoard(ds))
        env.set_persist_provider(PublishingCheckpoint(config.checkpoint, config.identity,
                                                     self, config.publication))
        for name in ["ORDER_PENDING_NEW", "ORDER_CREATION_PASS", "ORDER_CREATION_REJECT",
                     "ORDER_PENDING_CANCEL", "ORDER_CANCELLATION_PASS",
                     "ORDER_CANCELLATION_REJECT", "ORDER_UNSOLICITED_UPDATE"]:
            env.event_bus.add_listener(EVENT[name], self.order_event)

    def account_projection(self):
        account = self.env.portfolio.stock_account
        position = self.env.portfolio.get_position(SID, POSITION_DIRECTION.LONG)
        return {"cash": money(account.total_cash), "frozen_cash": money(account.frozen_cash),
                "equity": money(self.env.portfolio.total_value), "positions": [] if position.quantity == 0 else [{
                    "security": SID, "quantity": integer(position.quantity), "sellable": integer(position.closable),
                    "average_price": money(position.avg_price), "dividend_receivable": money(position.dividend_receivable)}]}

    @staticmethod
    def order_projection(order):
        status = {"PENDING_NEW": "pending_new", "PENDING_CANCEL": "pending_cancel",
                  "ACTIVE": "partially_filled" if order.filled_quantity else "accepted",
                  "FILLED": "filled", "REJECTED": "rejected", "CANCELLED": "cancelled"}[order.status.name]
        return {"order_id": str(order.order_id), "security": order.order_book_id,
                "side": order.side.name.lower(), "quantity": integer(order.quantity),
                "filled": integer(order.filled_quantity), "limit_price": money(order.price),
                "status": status, "reason": order.message or None}

    def append_step(self, kind, message, *, order=None, fill=None):
        context = self.env.user_strategy.user_context
        clock = self.env.calendar_dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        sequence = len(context.engine_events) + 1
        context.engine_events.append({
            "sequence": sequence, "logical_at": clock.isoformat(), "trading_day": clock.date().isoformat(),
            "kind": kind, "message": message, "account": self.account_projection(),
            "order": order, "fill": fill, "action_id": None,
            "checkpoint": {"native_identity": self.config.identity,
                           "native_input_cursor": self.env.event_source.cursor + 1,
                           "projection_sequence": sequence},
        })

    def order_event(self, event):
        order = getattr(event, "order", None)
        if order is None:
            # RQ may reject before creating an Order. Preserve native reason; invent no order ID.
            self.env.user_strategy.user_context.probes.append({
                "kind": "native_rejection", "event": event.event_type.name,
                "security": getattr(event, "order_book_id", None), "reason": getattr(event, "reason", None)})
        message = event.event_type.name + (": " + str(event.reason) if hasattr(event, "reason") else "")
        self.append_step("order", message, order=self.order_projection(order) if order is not None else None)

    def trade(self, event):
        super().trade(event)
        trade = event.trade
        self.append_step("fill", "Native TRADE", order=self.order_projection(event.order), fill={
            "fill_id": str(trade.exec_id), "order_id": str(trade.order_id), "security": trade.order_book_id,
            "side": trade.side.name.lower(), "quantity": integer(trade.last_quantity),
            "price": money(trade.last_price), "commission": money(trade.commission), "tax": money(trade.tax),
            "fee": money(trade.transaction_cost),
        })

    def observe(self, event):
        super().observe(event)
        kind = "close" if event.event_type in {EVENT.PRE_SETTLEMENT, EVENT.POST_SETTLEMENT} else "open"
        self.append_step(kind, event.event_type.name)

    def finish(self, event):
        self.append_step("close", "Native bounded replay finished")
        super().finish(event)

    def receipt(self):
        result = super().receipt()
        result.update(engine="RQAlpha", engine_version="6.4.1", scenario=self.config.scenario,
                      account=self.account_projection(), engine_events=self.env.user_strategy.user_context.engine_events,
                      real_market_validation="NOT_RUN", wire_money_precision="8 decimal places; native float projection")
        return result


def load_mod():
    return QuantMod()
