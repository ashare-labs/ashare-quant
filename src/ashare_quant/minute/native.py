"""Event/data adapters around the existing RQAlpha broker, matcher and Account."""

import inspect
import json
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION
from rqalpha.core.events import EVENT
from rqalpha.interface import AbstractMod, AbstractPriceBoard
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher import DefaultBarMatcher
from rqalpha.mod.rqalpha_mod_sys_simulation.matcher.base import OrderNotMatchable

from ashare_quant.contracts import SHANGHAI
from ashare_quant.daily_portfolio.native import Proxy
from ashare_quant.daily_portfolio.native import Source as DailySource
from rqprobe.events import RecordedEventSource
from rqprobe.guards import block_rqdata_apis

from .api import API, callback_name, order_view, validate_callback
from .inputs import require, stamp

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
        require(row is not None and row["_available"] <= at, "MINUTE_NATIVE_BAR_UNAVAILABLE")
        state = self.host.data.state(sid, str(at.date()), at=at)
        return {
            **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")},
            "datetime": int(dt.strftime("%Y%m%d%H%M%S")),
            "total_turnover": float(row["amount"]),
            "limit_up": float(state["upper_limit"]),
            "limit_down": float(state["lower_limit"]),
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
        return None  # explicit complete no-action coverage validated before native run

    def get_split(self, instrument):
        return None


class Board(AbstractPriceBoard):
    def __init__(self, host):
        self.host = host

    def get_last_price(self, sid):
        return self.host.data.current(sid, self.host.at)

    def get_limit_up(self, sid):
        return float(
            self.host.data.state(sid, str(self.host.at.date()), at=self.host.at)["upper_limit"]
        )

    def get_limit_down(self, sid):
        return float(
            self.host.data.state(sid, str(self.host.at.date()), at=self.host.at)["lower_limit"]
        )


class Matcher(DefaultBarMatcher):
    """Gate unavailable/same-interval bars; all fill/cash/volume code is native."""

    def __init__(self, host, config):
        self.host = host
        super().__init__(host.env, config, partial_fill_on_insufficient_cash=False)

    def _get_deal_price(self, order, instrument, open_auction=False):
        row = self.host.data.bars.get((order.order_book_id, self.host.at))
        submitted = order.datetime.replace(tzinfo=SHANGHAI)
        if (
            open_auction
            or row is None
            or row["_available"] > self.host.at
            or row["_start"] < submitted
            or self.host.at <= submitted
        ):
            raise OrderNotMatchable("AQ requires the next completed, available input interval")
        return super()._get_deal_price(order, instrument, open_auction)


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
        runtime["mod"] = self
        self.at = stamp(self.data.days[0] + "T09:00:00+08:00")
        self.phase = "initialize"
        self.orders, self.trades, self.accounts = {}, [], []
        self.seq = 0
        self.api = API(self)
        self.namespace = self.api.namespace()
        env.set_event_source(RecordedEventSource(env, runtime["records"]))
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
        if self.phase in {"before", "after"}:
            self.refresh_valuation()

    def refresh_valuation(self):
        # Account._on_bar uses this same public native Position update path.
        # Non-BAR clocks must also mark already visible prices; no extra BAR or
        # matching event is published, and no parallel equity calculation exists.
        for pos in self.env.portfolio.stock_account.get_positions():
            price = self.env.get_last_price(pos.order_book_id)
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
        with (runtime["root"] / "events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {"seq": self.seq, "at": self.at.isoformat(), "kind": kind, **payload},
                    ensure_ascii=False,
                    allow_nan=False,
                    default=str,
                )
                + "\n"
            )

    def initialize(self):
        from rqalpha.api import subscribe

        subscribe(list(self.data.ids))
        exec(
            compile(runtime["source"], str(runtime["root"] / "strategy.py"), "exec"), self.namespace
        )
        require(callable(self.namespace.get("initialize")), "JQ_INITIALIZE_REQUIRED")
        for name in ("initialize", "before_trading_start", "handle_data", "after_trading_end"):
            if name in self.namespace:
                validate_callback(self.namespace[name])
        self.invoke(self.namespace["initialize"])

    def invoke(self, callback, *args):
        validate_callback(callback)
        name = callback_name(callback)
        self.emit("callback_start", name=name, phase=self.phase)
        result = callback(self.api.context, *args)
        if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
            if inspect.iscoroutine(result):
                result.close()  # Do not evaluate it; avoid an unawaited-coroutine warning.
            raise ValueError(f"JQ_CALLBACK_RESULT_UNSUPPORTED:{name}:unevaluated_execution_object")
        self.emit("callback_end", name=name, phase=self.phase)

    def dispatch(self, phase):
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

    def order_event(self, event):
        o = getattr(event, "order", None)
        if o is not None:
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
            if pos.quantity:
                positions[sid] = SimpleNamespace(
                    security=sid,
                    total_amount=int(pos.quantity),
                    closeable_amount=int(pos.closable),
                    avg_cost=float(pos.avg_price),
                    price=float(pos.last_price),
                    value=float(pos.market_value),
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
        self.emit("account", **self.account())

    def close_day(self, event):
        self.refresh_valuation()
        self.accounts.append({"at": self.at.isoformat(), **self.account()})

    def finish(self, event):
        self.refresh_valuation()
        p = self.env.portfolio
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
            "model": "closed-bar-next-open-day-orders.v1",
            "orders": [
                {**vars(order_view(o)), "add_time": o.datetime.isoformat()}
                for o in self.orders.values()
            ],
            "trades": self.trades,
            "accounts": self.accounts,
            "final_account": self.account(),
            "preflight": self.data.preflight(),
        }

    def tear_down(self, code, exception=None):
        return getattr(self, "result", None)


def load_mod():
    return Mod()


def run_native(root, data, source, config, identity):
    import logbook
    import rqalpha.main as native
    import rqalpha.utils.config as config_module
    from rqalpha.environment import Environment
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade

    from ashare_quant.rq_mod import atomic_json
    from rqprobe.checkpoint import Counter

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
        root=root, data=data, source=source, config=config, identity=identity, records=records
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
                "matching_type": "next_bar",
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
                "auto_switch_order_value": False,
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
    wrapper = """from ashare_quant.minute.native import runtime
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
        if Environment._env is not None:
            native.cleanup_resources(Environment._env)
        atomic_json(root / "teardown.json", {"native_singleton_cleared": Environment._env is None})
