"""Event/data adapters around the existing RQAlpha broker, matcher and Account."""

import inspect
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
from rqalpha.const import INSTRUMENT_TYPE, ORDER_TYPE, POSITION_DIRECTION, SIDE
from rqalpha.core.events import EVENT
from rqalpha.interface import AbstractMod, AbstractPriceBoard
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import (
    OrderCancelled,
    OrderNotMatchable,
    OrderRejected,
)

from ashare_quant.backtests.minute.api import API, callback_name, order_view, validate_callback
from ashare_quant.backtests.minute.inputs import require, stamp
from ashare_quant.backtests.minute.lifecycle import JQEventSource, handle_clocks
from ashare_quant.contracts import SHANGHAI
from ashare_quant.execution.rqalpha.events import RecordedEventSource
from ashare_quant.execution.rqalpha.guards import block_rqdata_apis
from ashare_quant.portfolio.native import Proxy
from ashare_quant.portfolio.native import Source as DailySource
from ashare_quant.portfolio.participation import strict_lot_capacity

runtime = {}
__config__ = {"priority": 1000}


class Source(DailySource):
    def available_data_range(self, frequency):
        require(frequency == "1m", "MINUTE_NATIVE_FREQUENCY")
        return tuple(
            datetime.fromisoformat(d).date()
            for d in (self.host.data.days[0], self.host.data.days[-1])
        )

    def get_bar(self, instrument, dt, frequency):
        require(frequency == "1m", "MINUTE_NATIVE_FREQUENCY")
        sid, at = instrument.order_book_id, dt.replace(tzinfo=SHANGHAI)
        require(at == self.host.at, "MINUTE_NATIVE_BAR_CLOCK")
        row = self.host.data.bars.get((sid, at))
        if (sid, at) in self.host.data.gaps:
            return None  # Native unavailable bar; no invented OHLC/volume.
        require(row is not None and row["_available"] <= at, "MINUTE_NATIVE_BAR_UNAVAILABLE")
        lower, upper = self.host.data.price_bounds(sid, str(at.date()), at=at)
        return {
            **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")},
            "datetime": int(dt.strftime("%Y%m%d%H%M%S")),
            "total_turnover": float(row["amount"]),
            "limit_up": float(upper),
            "limit_down": float(lower),
        }

    def history_bars(self, instrument, bar_count, frequency, fields, dt, **kwargs):
        require(
            bar_count == 1 and frequency == "1d" and fields == "close",
            "MINUTE_NATIVE_HISTORY_SCOPE",
        )
        day = str(self.host.at.date())
        require(str(dt.date()) == self.host.data.previous[day], "MINUTE_PREVIOUS_CLOSE_DATE")
        return np.array(
            [
                float(
                    self.host.data.state(instrument.order_book_id, day, at=self.host.at)[
                        "previous_close"
                    ]
                )
            ]
        )

    def is_suspended(self, sid, dates):
        return [
            self.host.data.state(
                sid, str(d.date() if isinstance(d, datetime) else d), at=self.host.at
            )["suspended"]
            for d in dates
        ]

    def get_dividend(self, instrument):
        return None  # strict no-action coverage OR explicit bounded research admission

    def get_split(self, instrument):
        return None


class Board(AbstractPriceBoard):
    def __init__(self, host):
        self.host = host

    def get_last_price(self, sid):
        return self.host.data.current(sid, self.host.at)

    def get_limit_up(self, sid):
        return float(self.host.data.price_bounds(sid, str(self.host.at.date()), at=self.host.at)[1])

    def get_limit_down(self, sid):
        return float(self.host.data.price_bounds(sid, str(self.host.at.date()), at=self.host.at)[0])


class Matcher(DefaultBarMatcher):
    """Adapt price timing and exact capacity; retain native fills, costs and account."""

    def __init__(self, host, config):
        self.host = host
        self._attempted_at = {}
        self._capacity_clock = None
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def update(self, event):
        if (
            not self.host.config.get("lifecycle") == "jq_minute"
            or self._capacity_clock != self.host.at
        ):
            super().update(event)
            self._capacity_clock = self.host.at

    def match(self, account, order, open_auction):
        if self.host.config.get("capacity_policy") == "jq_daily_initial":
            with localcontext() as context:
                context.prec = 50
                return super().match(account, order, open_auction)
        return super().match(account, order, open_auction)

    def _get_deal_price(self, order, instrument, open_auction=False):
        reasons = self.host.data.execution_denial(order.order_book_id, self.host.at)
        if reasons:
            self.host.emit(
                "quote_use_denied",
                security=order.order_book_id,
                order_id=str(order.order_id),
                use="execution",
                reasons=reasons,
            )
            raise OrderNotMatchable(
                "Source-bound minute denial; order may continue at a later usable bar"
            )
        row = self.host.data.bars.get((order.order_book_id, self.host.at))
        submitted = order.datetime.replace(tzinfo=SHANGHAI)
        if self.host.config["matching"] == "jq_current":
            if (
                self.host.config.get("lifecycle") == "jq_minute"
                and submitted < self.host.at
                and self.host.phase != "bar"
            ):
                raise OrderNotMatchable("Resting matching follows the scheduled callbacks")
            if self.host.config.get("capacity_policy") == "jq_daily_initial":
                if self._attempted_at.get(order.order_id) == self.host.at:
                    raise OrderNotMatchable("Per-order capacity already attempted at this clock")
                if not open_auction and submitted == self.host.at and row is None:
                    return self.host.data.boundary_execution_price(
                        order.order_book_id, self.host.at
                    )
            if open_auction or row is None or row["_available"] > self.host.at:
                raise OrderNotMatchable("JQ-current requires a completed visible bar at this clock")
            if submitted == self.host.at:
                # No future open/high/low is consulted for a new order.
                return float(row["close"])
            if row["_start"] < submitted or self.host.at <= submitted:
                raise OrderNotMatchable("Resting orders require a later complete interval")
            if order.type == ORDER_TYPE.LIMIT:
                crossed = (
                    float(row["low"]) < order.price
                    if order.side == SIDE.BUY
                    else float(row["high"]) > order.price
                )
                if not crossed:
                    raise OrderNotMatchable("Resting limit requires strict low/high crossing")
                # JQ documented resting-bar rule. Native matcher still checks
                # limits, capacity, cash, T+1, costs and publishes the real Trade.
                return order.price
            raise OrderNotMatchable("JQ-current market orders do not carry to later bars")
        if (
            open_auction
            or row is None
            or row["_available"] > self.host.at
            or row["_start"] < submitted
            or self.host.at <= submitted
        ):
            raise OrderNotMatchable("AQ requires the next completed, available input interval")
        return super()._get_deal_price(order, instrument, open_auction)

    def _get_liquidity_limited_fill(self, order, instrument, open_auction=False):
        if self.host.config.get("lifecycle") == "jq_minute":
            self.update(None)
        if self.host.config.get("capacity_policy") == "jq_daily_initial":
            require(self._volume_limit and not open_auction, "JQ_EXPLICIT_CAPACITY_REQUIRED")
            initial = order.datetime.replace(tzinfo=SHANGHAI) == self.host.at
            bar = self.host.data.bars.get((order.order_book_id, self.host.at))
            zero_minute = bar is not None and Fraction(str(bar["volume"])) == 0
            if zero_minute:
                row, cap = bar, 0
            elif initial:
                row = self.host.data.execution_daily_volume(
                    order.order_book_id, str(self.host.at.date())
                )
                raw = Fraction(str(row["volume"])) * Fraction(
                    str(self.host.config["volume_percent"])
                )
                cap = int(raw // instrument.round_lot) * instrument.round_lot
            else:
                row = self.host.data.bars[order.order_book_id, self.host.at]
                raw = Fraction(str(row["volume"])) * Fraction(
                    str(self.host.config["volume_percent"])
                )
                cap = int(raw)
            self._attempted_at[order.order_id] = self.host.at
            self.host.emit(
                "volume_capacity",
                security=order.order_book_id,
                order_id=str(order.order_id),
                policy="jq_daily_initial",
                stage="zero_volume_minute"
                if zero_minute
                else ("initial_daily" if initial else "resting_minute"),
                source_volume=row["volume"],
                volume_percent=self.host.config["volume_percent"],
                per_order_cap=cap,
                denominator_strategy_visible=zero_minute or not initial,
                denominator_available_at=row["_available"].isoformat(),
            )
            if cap == 0:
                if order.type == ORDER_TYPE.MARKET:
                    raise OrderCancelled("Explicit JQ per-order participation budget exhausted")
                raise OrderNotMatchable("Explicit JQ per-order participation budget exhausted")
            return min(order.unfilled_quantity, cap)
        native_fill = super()._get_liquidity_limited_fill(order, instrument, open_auction)
        require(self._volume_limit and not open_auction, "MINUTE_STRICT_VOLUME_POLICY_REQUIRED")
        row = self.host.data.bars[order.order_book_id, self.host.at]
        consumed = self._turnover[order.order_book_id]
        cap = strict_lot_capacity(
            row["volume"], self.host.config["volume_percent"], instrument.round_lot, consumed
        )
        self.host.emit(
            "volume_capacity",
            security=order.order_book_id,
            order_id=str(order.order_id),
            source_volume=row["volume"],
            volume_percent=self.host.config["volume_percent"],
            consumed=consumed,
            strict_remaining=cap,
            native_proposal=int(native_fill),
        )
        if cap == 0:
            if order.type == ORDER_TYPE.MARKET:
                raise OrderCancelled("Strict whole-lot minute participation budget exhausted")
            raise OrderNotMatchable("Strict whole-lot minute participation budget exhausted")
        return min(native_fill, cap)

    def _resolve_open_fill(self, account, order, instrument, price, fill):
        if self.host.config.get("capacity_policy") != "jq_daily_initial":
            return super()._resolve_open_fill(account, order, instrument, price, fill)

        # Use the existing native reservation, including retained price improvement.
        # Native matcher/Account still calculate and apply the actual Trade costs.
        def d(value):
            return Decimal(str(value))

        remaining_value = order.unfilled_quantity * d(price)
        prior_value = order.filled_quantity * d(order.avg_price)
        total_fee = max(
            d(self.host.config["min_commission"]),
            (prior_value + remaining_value) * d(self.host.config["commission_rate"]),
        )
        required = remaining_value + max(Decimal(0), total_fee - d(order.transaction_cost))
        available = d(account.cash) + d(account._remaining_frozen_cash_of_order(order))
        if required - available > Decimal("1e-8"):
            reason = "Explicit JQ order exceeds available cash and its native remaining reservation"
            if order.filled_quantity:
                raise OrderCancelled(reason)
            raise OrderRejected(reason)
        return fill


class BarData(Mapping):
    def __init__(self, host):
        self.host = host
        self.ids = tuple(host.api.universe)

    def __iter__(self):
        return iter(self.ids)

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, sid):
        require(sid in self.ids, "JQ_SECURITY_OUT_OF_SCOPE")
        rows = self.host.data.visible(sid, self.host.at, count=1)
        require(rows, f"JQ_NO_VISIBLE_BAR:{sid}")
        row = rows[-1]
        return SimpleNamespace(
            **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")},
            money=float(row["amount"]),
            datetime=row["_end"].replace(tzinfo=None),
        )


class Mod(AbstractMod):
    def start_up(self, env, config):
        self.env, self.data, self.config = env, runtime["data"], runtime["config"]
        if self.data.corporate_actions is not None:
            from ashare_quant.portfolio.corporate_native import install

            install(self)
        runtime["mod"] = self
        self.jq_lifecycle = self.config["lifecycle"] == "jq_minute"
        self.jq_capacity = self.config["capacity_policy"] == "jq_daily_initial"
        self.at = stamp(
            self.data.days[0] + ("T00:00:00+08:00" if self.jq_lifecycle else "T09:00:00+08:00")
        )
        self.parameters = runtime.get("parameters", {})
        self.phase = "initialize"
        self.orders, self.trades, self.accounts = {}, [], []
        self.order_origin_cost = {}
        self.seq = 0
        self.api = API(self)
        self.namespace = self.api.namespace()
        env.set_event_source(
            JQEventSource(self.data)
            if self.jq_lifecycle
            else RecordedEventSource(env, runtime["records"])
        )
        if self.jq_capacity:
            from ashare_quant.execution.reservation import install_budget_account

            install_budget_account(env)
        if self.jq_lifecycle:
            self.handle_times = {d: set(handle_clocks(d)) for d in self.data.days}
            env.event_bus.add_listener(EVENT.USER, self.user_clock)
        env.set_data_source(Source(self))
        env.set_price_board(Board(self))
        env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self.ready)
        env.event_bus.add_listener(EVENT.TRADE, self.trade)
        for kind in (
            EVENT.ORDER_PENDING_NEW,
            EVENT.ORDER_CREATION_PASS,
            EVENT.ORDER_CREATION_REJECT,
            EVENT.ORDER_UNSOLICITED_UPDATE,
            EVENT.ORDER_CANCELLATION_PASS,
        ):
            env.event_bus.add_listener(kind, self.order_event)
        env.event_bus.add_listener(EVENT.POST_BAR, self.snapshot)
        env.event_bus.add_listener(EVENT.POST_AFTER_TRADING, self.close_day)
        env.event_bus.add_listener(EVENT.POST_STRATEGY_RUN, self.finish)
        block_rqdata_apis()

    @property
    def day(self):
        return str(self.at.date())

    def ready(self, event):
        self.env.broker.register_matcher(
            INSTRUMENT_TYPE.CS, Matcher(self, self.env.config.mod.sys_simulation)
        )
        for kind in (EVENT.PRE_BEFORE_TRADING, EVENT.PRE_BAR, EVENT.PRE_AFTER_TRADING):
            self.env.event_bus.prepend_listener(kind, self.clock)

    def clock(self, event):
        self.at = self.env.trading_dt.replace(tzinfo=SHANGHAI)
        self.phase = {
            EVENT.PRE_BEFORE_TRADING: "before",
            EVENT.PRE_BAR: "bar",
            EVENT.PRE_AFTER_TRADING: "after",
        }[event.event_type]
        self.emit("clock", phase=self.phase)
        if self.phase == "before" and self.jq_lifecycle:
            for order in list(self.env.broker.get_open_orders()):
                require(order.datetime.date() < self.at.date(), "JQ_ROLLOVER_ORDER_DATE")
                self.emit(
                    "order_expiry_boundary",
                    order_id=str(order.order_id),
                    policy="before-next-session-callback",
                    platform_exact_cancel_time_verified=False,
                )
                with localcontext() as context:
                    context.prec = 50
                    self.env.broker.cancel_order(order)
        if self.phase in {"before", "after"}:
            self.refresh_valuation()

    def user_clock(self, event):
        """Dispatch only adapter clocks; they never pretend to be native bars."""
        from rqalpha.const import EXECUTION_PHASE
        from rqalpha.core.execution_context import ExecutionContext

        phase = getattr(event, "minute_phase", None)
        require(phase in {"scheduled", "after", "end"}, "JQ_UNKNOWN_CLOCK_EVENT")
        self.env.update_time(event.calendar_dt, event.trading_dt)
        self.at = event.trading_dt.replace(tzinfo=SHANGHAI)
        self.phase = phase
        self.emit("clock", phase=phase)
        self.refresh_valuation()
        execution = (
            EXECUTION_PHASE.ON_BAR if phase == "scheduled" else EXECUTION_PHASE.AFTER_TRADING
        )
        with ExecutionContext(execution):
            if phase == "end":
                if "on_strategy_end" in self.namespace:
                    self.invoke(self.namespace["on_strategy_end"])
            else:
                self.dispatch(phase)
            if phase == "scheduled" and self.at not in self.data.ends[str(self.at.date())]:
                self.dispatch("bar")
        if phase == "after":
            self.close_day(event)

    def refresh_valuation(self):
        # Account._on_bar uses this same public native Position update path.
        # Non-BAR clocks must also mark already visible prices; no extra BAR or
        # matching event is published, and no parallel equity calculation exists.
        for pos in self.env.portfolio.stock_account.get_positions():
            price = self.data.valuation_price(pos.order_book_id, self.at)
            previous = pos.last_price
            pos.update_last_price(price)
            if previous != price:
                self.emit(
                    "valuation_refresh",
                    security=pos.order_book_id,
                    previous_price=float(previous),
                    price=float(price),
                    phase=self.phase,
                )

    def emit(self, kind, **payload):
        self.seq += 1
        stream = runtime.get("event_stream")
        if stream is None:
            stream = runtime["event_stream"] = (runtime["root"] / "events.jsonl").open("a")
        stream.write(
            json.dumps(
                {"seq": self.seq, "at": self.at.isoformat(), "kind": kind, **payload},
                ensure_ascii=False,
                allow_nan=False,
                default=str,
            )
            + "\n"
        )
        # Preserve visibility of every completed event, including when the next
        # callback fails or the worker is killed. Only the append handle is reused.
        stream.flush()

    def initialize(self):
        from rqalpha.api import subscribe

        subscribe(list(self.data.ids))
        exec(
            compile(runtime["source"], str(runtime["root"] / "strategy.py"), "exec"), self.namespace
        )
        require(callable(self.namespace.get("initialize")), "JQ_INITIALIZE_REQUIRED")
        for name in (
            "initialize",
            "before_trading_start",
            "handle_data",
            "after_trading_end",
            "on_strategy_end",
        ):
            if name in self.namespace:
                validate_callback(self.namespace[name])
        self.invoke(self.namespace["initialize"])

    def invoke(self, callback, *args):
        validate_callback(callback)
        name = callback_name(callback)
        self.emit("callback_start", name=name, phase=self.phase)
        # Upstream imports lower the worker's global Decimal precision to 10.
        # All callback modes need the same stable arithmetic: equal moving
        # averages can otherwise become unequal and submit a spurious order.
        with localcontext() as context:
            context.prec = 50
            result = callback(self.api.context, *args)
        if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
            if inspect.iscoroutine(result):
                result.close()  # Do not evaluate it; avoid an unawaited-coroutine warning.
            raise ValueError(f"JQ_CALLBACK_RESULT_UNSUPPORTED:{name}:unevaluated_execution_object")
        self.emit("callback_end", name=name, phase=self.phase)

    def dispatch(self, phase):
        if self.jq_lifecycle:
            self.dispatch_jq(phase)
            return
        name = {
            "before": "before_trading_start",
            "bar": "handle_data",
            "after": "after_trading_end",
        }[phase]
        actual_bar = self.at in self.data.ends[str(self.at.date())]
        if phase == "before" and name in self.namespace:
            self.invoke(self.namespace[name])
        for callback, clock in self.api.schedules:
            if clock == self.at.strftime("%H:%M") or (
                clock == "every_bar" and phase == "bar" and actual_bar
            ):
                self.invoke(callback)
        if phase == "bar" and actual_bar and name in self.namespace:
            self.invoke(self.namespace[name], BarData(self))
        if phase == "after" and name in self.namespace:
            self.invoke(self.namespace[name])

    def dispatch_jq(self, phase):
        if phase == "before" and "before_trading_start" in self.namespace:
            self.invoke(self.namespace["before_trading_start"])
        is_handle = self.at in self.handle_times[str(self.at.date())]
        for callback, clock in self.api.schedules:
            if (
                phase in {"before", "scheduled", "after"} and clock == self.at.strftime("%H:%M")
            ) or (phase == "bar" and is_handle and clock == "every_bar"):
                self.invoke(callback)
        if phase == "bar" and is_handle and "handle_data" in self.namespace:
            self.invoke(self.namespace["handle_data"], BarData(self))
        if phase == "after" and "after_trading_end" in self.namespace:
            self.invoke(self.namespace["after_trading_end"])

    def order_event(self, event):
        o = getattr(event, "order", None)
        if o is not None:
            if str(o.order_id) not in self.order_origin_cost and o.side == SIDE.SELL:
                pos = self.env.portfolio.get_position(o.order_book_id, POSITION_DIRECTION.LONG)
                self.order_origin_cost[str(o.order_id)] = float(pos.avg_price)
            self.orders[str(o.order_id)] = o
            self.emit("order_event", event=event.event_type.name, **vars(order_view(o)))
        else:
            self.emit(
                "order_rejection",
                security=getattr(event, "order_book_id", None),
                reason=getattr(event, "reason", str(event)),
            )

    def trade(self, event):
        t = event.trade
        row = dict(
            trade_id=str(t.exec_id),
            order_id=str(t.order_id),
            security=t.order_book_id,
            at=self.at.isoformat(),
            amount=int(t.last_quantity),
            side=t.side.name,
            price=float(t.last_price),
            commission=float(t.commission),
            tax=float(t.tax),
        )
        self.trades.append(row)
        self.emit("trade", **row)

    def portfolio_view(self):
        p, account = self.env.portfolio, self.env.portfolio.stock_account
        positions = {}
        for sid in self.data.ids:
            pos = p.get_position(sid, POSITION_DIRECTION.LONG)
            corporate = self.data.corporate_actions is not None
            if pos.quantity or (corporate and pos.dividend_receivable):
                positions[sid] = SimpleNamespace(
                    security=sid,
                    total_amount=int(pos.quantity),
                    closeable_amount=int(pos.closable),
                    avg_cost=float(pos.avg_price),
                    price=float(pos.last_price),
                    value=float(pos.market_value),
                    **(
                        {"dividend_receivable": float(pos.dividend_receivable)} if corporate else {}
                    ),
                    **(
                        {"valuation": self.data.valuation_mark(sid, self.at)}
                        if self.data.generic_research
                        else {}
                    ),
                    **(
                        {"today_amount": int(pos.get_state()["non_closable"])}
                        if self.jq_lifecycle
                        else {}
                    ),
                )
        return SimpleNamespace(
            available_cash=float(account.cash),
            locked_cash=float(account.frozen_cash),
            total_value=float(p.total_value),
            positions_value=float(p.market_value),
            starting_cash=self.config["initial_cash"],
            positions=positions,
            long_positions=positions,
        )

    def account(self):
        view = self.portfolio_view()
        return {
            k: {sid: vars(p) for sid, p in v.items()} if k == "positions" else v
            for k, v in vars(view).items()
            if k != "long_positions"
        }

    def snapshot(self, event):
        if self.data.full_session_input:
            self.refresh_valuation()
        self.emit("account", **self.account())

    def close_day(self, event):
        self.refresh_valuation()
        for action in self.data.corporate_actions or []:
            if action["record_date"] == self.day:
                self.env.portfolio.stock_account._get_or_create_pos(
                    action["security"], POSITION_DIRECTION.LONG
                ).record(action)
        self.accounts.append({"at": self.at.isoformat(), **self.account()})

    def finish(self, event):
        self.refresh_valuation()
        p = self.env.portfolio
        if self.data.research and not self.data.full_session_input:
            require(
                not self.account()["positions"], "MINUTE_RESEARCH_MUST_EXIT_WITHIN_ACTION_SCOPE"
            )
        self.result = {
            "schema": "aq-python-minute-result.v1",
            "identity": runtime["identity"],
            "real_strategy_executed": True,
            "is_test_double": False,
            "real_market_data": self.data.document["real_market_data"],
            "historical_pit_verified": False,
            "state_visibility": self.data.state_visibility,
            "state_replay_used": self.data.state_replay_used,
            "jq_comparison": "NOT_PERFORMED",
            "native_classes": {
                "broker": type(self.env.broker).__module__ + "." + type(self.env.broker).__name__,
                "account": type(p.stock_account).__module__ + "." + type(p.stock_account).__name__,
            },
            "model": (
                "jq-minute-daily-initial-resting-cap.v1"
                if self.jq_capacity
                else (
                    "closed-bar-next-open-day-orders.v1"
                    if self.config["matching"] == "next_open"
                    else "jq-current-close-resting-limit-minute-cap.v1"
                )
            ),
            "execution_policy": {
                "matching": self.config["matching"],
                "cash_policy": self.config["cash_policy"],
                "lifecycle": self.config["lifecycle"],
                "capacity": "jq-per-order-daily-initial-minute-resting.v1"
                if self.jq_capacity
                else "exact-whole-lot-per-minute-shared.v1",
                "capacity_policy": self.config["capacity_policy"],
                "retrospective_daily_denominator": self.jq_capacity,
                "jq_daily_order_volume_cap_verified": False,
                "reservation": "native-buy-limit-budget"
                if self.jq_capacity
                else "native-proportional",
                "day_order_expiry": "before-next-session-callback"
                if self.jq_lifecycle
                else "native-after-trading",
                "platform_exact_cancel_time_verified": False,
                **({"terminal_day_queries": "empty"} if self.jq_lifecycle else {}),
                **(
                    {
                        "opening_execution_price": "visible-same-day-opening-scalar-only",
                        "resume_execution_price": "visible-same-day-closed-minute-only",
                        "zero_volume_execution": "no-fills-at-visible-zero-volume-minute",
                        "unverified_jq_rules": [
                            "exact_initial_daily_denominator_not_uniquely_identified",
                            "resting_multiple_orders_use_independent_capacity",
                            "equal_touch_and_low_high_cross_without_close_cross",
                            "sell_side_symmetry",
                            "1130_and_1500_matching_internal_phase",
                            "zero_volume_initial_order_handling",
                            "exact_day_order_cancellation_clock",
                        ],
                    }
                    if self.jq_capacity
                    else {}
                ),
                "initial_price": "next_open"
                if self.config["matching"] == "next_open"
                else "visible_close",
                "resting_limit_price": "next_open"
                if self.config["matching"] == "next_open"
                else "order_limit_strict_cross",
            },
            "orders": [
                {**vars(order_view(o)), "add_time": o.datetime.isoformat()}
                for o in self.orders.values()
            ],
            "trades": self.trades,
            "accounts": self.accounts,
            "final_account": self.account(),
            "total_pnl": float(p.total_value) - self.config["initial_cash"],
            "total_return": float(p.total_value) / self.config["initial_cash"] - 1,
            "preflight": self.data.preflight(),
            **(
                {
                    "corporate_account_model": self.data.document["corporate_account_model"],
                    "corporate_schedule_sha256": self.data.preflight()["corporate_schedule_sha256"],
                    "corporate_entitlements": {
                        sid: self.env.portfolio.get_position(
                            sid, POSITION_DIRECTION.LONG
                        ).entitlements
                        for sid in self.data.ids
                    },
                }
                if self.data.corporate_actions is not None
                else {}
            ),
        }

    def tear_down(self, code, exception=None):
        return getattr(self, "result", None)


def load_mod():
    return Mod()


def run_native(root, data, source, config, identity, *, parameters=None):
    import logbook
    import rqalpha.main as native
    import rqalpha.utils.config as config_module
    from rqalpha.environment import Environment
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade

    from ashare_quant.execution.rqalpha.checkpoint import Counter
    from ashare_quant.execution.synthetic.mod import atomic_json

    records = []
    for d in data.days:
        events = [
            ("BEFORE_TRADING", stamp(d + "T09:00:00+08:00")),
            *(("BAR", at) for at in data.clocks[d]),
            ("AFTER_TRADING", stamp(d + "T15:30:00+08:00")),
        ]
        offset = len(records)
        records.extend(
            {"cursor": offset + i + 1, "event": kind, "dt": at.replace(tzinfo=None).isoformat()}
            for i, (kind, at) in enumerate(events)
        )
    runtime.update(
        root=root, data=data, source=source, config=config, identity=identity, records=records, parameters=parameters or {}
    )
    requested = {
        "base": {
            "start_date": data.days[0],
            "end_date": data.days[-1],
            "frequency": "1m",
            "accounts": {"stock": config["initial_cash"]},
            "run_type": "b",
            "rqdatac_uri": "disabled",
            "auto_update_bundle": False,
            "data_bundle_path": str(root / "ABSENT_BUNDLE"),
            "persist": False,
            "capital_gain_tax_rate": 0,
            "partial_fill_on_insufficient_cash": False,
        },
        "extra": {"log_level": "error", "locale": "en_US"},
        "mod": {
            "sys_progress": {"enabled": False},
            "sys_analyser": {"enabled": False},
            "sys_scheduler": {"enabled": False},
            "sys_simulation": {
                "matching_type": "next_bar" if config["matching"] == "next_open" else "current_bar",
                "signal": False,
                "price_limit": True,
                "volume_limit": True,
                "volume_percent": config["volume_percent"],
                "inactive_limit": False,
                "slippage": 0,
                "management_fee": [],
            },
            "sys_accounts": {
                "stock_t1": True,
                "validate_stock_position": True,
                "auto_switch_order_value": config["cash_policy"] == "resize",
                "financing_rate": 0,
                "financing_stocks_restriction_enabled": False,
                "dividend_reinvestment": False,
            },
            "sys_risk": {
                "validate_price": True,
                "validate_is_trading": True,
                "validate_cash": True,
            },
            "sys_transaction_cost": {
                "stock_min_commission": config["min_commission"],
                "stock_commission_multiplier": config["commission_rate"] / 0.0008,
                "tax_multiplier": config["sell_tax"] / 0.0005,
                "pit_tax": False,
            },
            "pythonminute": {"enabled": True, "lib": __name__, "priority": 1000},
        },
    }
    wrapper = """from ashare_quant.backtests.minute.native import runtime
def init(context): runtime["mod"].initialize()
def before_trading(context): runtime["mod"].dispatch("before")
def handle_bar(context, bars): runtime["mod"].dispatch("bar")
def after_trading(context): runtime["mod"].dispatch("after")
"""
    config_module.user_config = lambda: {}
    config_module.project_config = lambda: {}
    parsed = config_module.parse_config(requested, source_code=wrapper)
    enabled = {k for k, v in parsed.mod.convert_to_dict().items() if v["enabled"]}
    require(
        enabled
        == {"sys_accounts", "sys_risk", "sys_simulation", "sys_transaction_cost", "pythonminute"},
        "MINUTE_UNEXPECTED_NATIVE_PLUGIN",
    )
    Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)

    def forbidden(*args, **kwargs):
        raise RuntimeError("MINUTE_DEFAULT_BUNDLE_FORBIDDEN")

    native.BaseDataSource, native.DataProxy = forbidden, Proxy
    atomic_json(root / "native-config.json", requested)
    try:
        with logbook.FileHandler(str(root / "native.log")).applicationbound():
            result = native.run(parsed, source_code=wrapper)
        require(result and result.get("pythonminute"), "MINUTE_NATIVE_COMPLETION_MISSING")
        return result["pythonminute"]
    finally:
        try:
            if Environment._env is not None:
                native.cleanup_resources(Environment._env)
            atomic_json(root / "teardown.json", {"native_singleton_cleared": Environment._env is None})
        finally:
            stream = runtime.pop("event_stream", None)
            if stream is not None:
                stream.close()
