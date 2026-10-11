"""Read-only JQ-shaped projections and calls into the sole native account.

The original trusted script executes unchanged. Unsupported attributes remain
missing; they are not guessed from the reference. No strategy-side sizing.
"""

import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace

import pandas as pd
from rqalpha.const import POSITION_DIRECTION, SIDE

from .owner import OwnerError


class FutureDataError(RuntimeError):
    pass


class MarketOrderStyle:
    pass


class LimitOrderStyle:
    def __init__(self, price):
        self.price = price


class NativeOrder:
    def __init__(self, order, cost=None):
        self.native = order
        self.cost = cost

    @property
    def order_id(self):
        return str(self.native.order_id)

    @property
    def security(self):
        return self.native.order_book_id

    @property
    def add_time(self):
        return self.native.datetime

    @property
    def is_buy(self):
        return self.native.side == SIDE.BUY

    @property
    def amount(self):
        return int(self.native.quantity)

    @property
    def filled(self):
        return int(self.native.filled_quantity)

    @property
    def status(self):
        if self.native.is_active() and self.native.filled_quantity:
            return "filled"
        return {
            "PENDING_NEW": "open",
            "ACTIVE": "open",
            "FILLED": "held",
            "CANCELLED": "canceled",
            "REJECTED": "rejected",
        }[self.native.status.name]

    @property
    def price(self):
        return self.native.avg_price

    @property
    def commission(self):
        return self.native.transaction_cost

    @property
    def avg_cost(self):
        return self.native.avg_price if self.is_buy else self.cost

    @property
    def side(self):
        return "long"

    @property
    def action(self):
        return "open" if self.is_buy else "close"

    def __str__(self):
        return f"NativeOrder(id={self.order_id}, status={self.status}, amount={self.amount}, filled={self.filled})"


def position(p):
    return SimpleNamespace(
        security=p.order_book_id,
        total_amount=int(p.quantity),
        closeable_amount=int(p.closable),
        today_amount=int(p._non_closable),
        locked_amount=int(p.quantity - p.closable - p._non_closable),
        price=p.last_price,
        avg_cost=p.avg_price,
        value=p.market_value,
        side="long",
    )


class Portfolio:
    def __init__(self, host):
        self.h = host

    @property
    def starting_cash(self):
        return self.h.case.initial_cash

    @property
    def inout_cash(self):
        # Only initial funding is admitted by this bounded facade.
        return self.starting_cash

    @property
    def available_cash(self):
        return self.h.env.portfolio.stock_account.cash

    @property
    def locked_cash(self):
        return self.h.env.portfolio.stock_account.frozen_cash

    @property
    def positions_value(self):
        return self.h.env.portfolio.stock_account.market_value

    @property
    def total_value(self):
        return self.h.env.portfolio.stock_account.total_value

    @property
    def returns(self):
        return self.total_value / self.starting_cash - 1

    @property
    def positions(self):
        return {
            p.order_book_id: position(p)
            for p in self.h.env.portfolio.stock_account.get_positions()
            if p.quantity
        }


class Context:
    def __init__(self, host):
        self.h = host
        self.portfolio = Portfolio(host)
        self.run_params = SimpleNamespace(
            start_date=host.case.config["start_date"],
            end_date=host.case.end_day,
            frequency="minute",
            type="full_backtest",
        )

    @property
    def current_dt(self):
        return self.h.at.replace(tzinfo=None)

    @property
    def previous_date(self):
        return self.h.previous[str(self.h.at.date())]


class Current:
    def __init__(self, h):
        self.h = h

    def __getitem__(self, sid):
        self.h.security(sid)
        value = self.h.owner.call(
            "current", security=sid, as_of=self.h.at.isoformat(), role="strategy"
        )
        if value["status"] != "available":
            raise RuntimeError("CURRENT_OBSERVATION_MISSING")
        data = value["rows"][0]["values"]
        return SimpleNamespace(**{k: v if type(v) is bool else float(v) for k, v in data.items()})


def install(h):
    mod = ModuleType("jqdata")
    mod.g = SimpleNamespace()
    mod.MarketOrderStyle, mod.LimitOrderStyle = MarketOrderStyle, LimitOrderStyle
    mod.FixedSlippage = lambda x: SimpleNamespace(value=x)
    mod.OrderCost = lambda **kwargs: SimpleNamespace(**kwargs)
    mod.log = SimpleNamespace(
        info=lambda text: None, error=lambda text: h.emit("recorder_error", message=text)
    )
    mod.get_current_data = lambda: Current(h)
    mod.set_benchmark = lambda x: None
    mod.set_option = lambda k, v: h.check_option(k, v)
    mod.set_slippage = lambda x: h.check_option("fixed_slippage", x.value)
    mod.set_order_cost = lambda cost, type: h.check_cost(vars(cost), type)

    def schedule(callback, time, reference_security):
        if time not in h.case.config["probe_times"] or reference_security != h.case.security:
            raise RuntimeError("SECOND_CALLBACK_CONFIG_MISMATCH")
        h.callbacks[time] = callback

    mod.run_daily = schedule

    def price(**params):
        params = {k: str(v) if isinstance(v, datetime) else v for k, v in params.items()}
        try:
            answer = h.owner.call("query", parameters=params, as_of=h.at.isoformat())
        except OwnerError as exc:
            if exc.code in {"JQ_FUTURE_QUERY", "JQ_INCOMPLETE_DAILY"}:
                raise FutureDataError(str(exc)) from exc
            raise
        if answer["status"] != "available":
            raise RuntimeError("QUERY_OBSERVATION_MISSING_OR_SOURCE_ERROR")
        rows = answer["rows"]
        frame = pd.DataFrame(
            [{k: float(v) for k, v in r.items() if k != "index"} for r in rows],
            index=pd.to_datetime([r["index"] for r in rows]),
        )
        return frame[params["fields"]]

    mod.get_price = price

    def orders(open_only=False):
        return {
            str(o.order_id): NativeOrder(o, h.order_costs.get(str(o.order_id)))
            for o in h.orders.values()
            if str(o.datetime.date()) == str(h.at.date()) and (not open_only or o.is_active())
        }

    mod.get_orders = orders
    mod.get_open_orders = lambda: orders(True)
    mod.get_trades = lambda: {
        t["trade_id"]: SimpleNamespace(
            trade_id=t["trade_id"],
            order_id=t["order_id"],
            security=t["security"],
            time=datetime.fromisoformat(t["at"]).replace(tzinfo=None),
            amount=t["quantity"],
            price=float(t["price"]),
        )
        for t in h.trades
        if t["at"][:10] == str(h.at.date())
    }

    def order(sid, amount, style):
        from rqalpha.api import order_shares
        from rqalpha.model.order import LimitOrder, MarketOrder

        h.security(sid)
        h.submissions += 1
        h.emit(
            "original_order_request",
            security=sid,
            quantity=amount,
            limit=None if isinstance(style, MarketOrderStyle) else style.price,
        )
        if h.case.policy in {"fixed", "integrated"} and amount < 0:
            from .policies import sell_quantity

            closable = int(h.env.portfolio.get_position(sid, POSITION_DIRECTION.LONG).closable)
            adjusted = sell_quantity(amount, closable)
            h.emit(
                "native_sell_quantity_policy",
                raw_quantity=amount,
                closable=closable,
                admitted_quantity=adjusted,
                policy="cap_at_native_closable",
            )
            amount = adjusted
            if amount == 0:
                return None
        native_style = (
            MarketOrder() if isinstance(style, MarketOrderStyle) else LimitOrder(style.price)
        )
        cost = (
            h.env.portfolio.get_position(sid, POSITION_DIRECTION.LONG).avg_price
            if amount < 0
            else None
        )
        result = order_shares(sid, amount, price_or_style=native_style)
        if result is not None:
            h.orders[str(result.order_id)] = result
            h.order_costs[str(result.order_id)] = cost
        return None if result is None else NativeOrder(result, cost)

    mod.order = order

    def cancel(o):
        from rqalpha.api import cancel_order

        cancel_order(o.native)
        return o

    mod.cancel_order = cancel

    def write_file(name, content, append=False):
        if append:
            raise RuntimeError("APPEND_UNSUPPORTED")
        (h.root / "original-events.jsonl").write_text(content)

    mod.write_file = write_file
    mod.__all__ = [k for k in vars(mod) if not k.startswith("_")]
    sys.modules["jqdata"] = mod
    return mod
