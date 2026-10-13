"""Explicit JQ-style subset; functions delegate orders to native RQAlpha APIs."""

import inspect
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import ModuleType, SimpleNamespace

from .inputs import number, require, stamp


class UnsupportedAPI(NotImplementedError):
    pass


def callback_name(callback):
    return getattr(callback, "__name__", type(callback).__name__)


def validate_callback(callback):
    require(callable(callback), "JQ_CALLBACK_REQUIRED")
    targets = (callback, getattr(callback, "__call__", None))
    require(
        not any(
            inspect.iscoroutinefunction(f)
            or inspect.isgeneratorfunction(f)
            or inspect.isasyncgenfunction(f)
            for f in targets
        ),
        f"JQ_CALLBACK_UNSUPPORTED:{callback_name(callback)}:synchronous_function_required",
    )


def unsupported(name):
    def fail(*args, **kwargs):
        raise UnsupportedAPI(f"JQ_API_UNSUPPORTED:{name}; see docs/python-minute.md")

    return fail


class OrderStatus(str, Enum):
    new = "new"
    held = "held"
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"


@dataclass(frozen=True)
class LimitOrderStyle:
    price: float

    def __post_init__(self):
        number(self.price)


@dataclass(frozen=True)
class MarketOrderStyle:
    pass


@dataclass(frozen=True)
class FixedSlippage:
    value: float


@dataclass(frozen=True)
class OrderCost:
    open_tax: float = 0
    close_tax: float = 0.001
    open_commission: float = 0.0003
    close_commission: float = 0.0003
    close_today_commission: float = 0
    min_commission: float = 5


def order_view(order):
    statuses = {
        "PENDING_NEW": "new",
        "ACTIVE": "held",
        "FILLED": "filled",
        "CANCELLED": "canceled",
        "REJECTED": "rejected",
    }
    return SimpleNamespace(
        order_id=str(order.order_id),
        security=order.order_book_id,
        amount=int(order.quantity),
        filled=int(order.filled_quantity),
        is_buy=order.side.name == "BUY",
        status=OrderStatus(statuses[order.status.name]),
        price=float(order.price),
        avg_cost=float(order.avg_price),
        add_time=order.datetime,
        native_status=order.status.name,
        message=order.message,
    )


class Context:
    def __init__(self, host):
        self._host = host

    @property
    def current_dt(self):
        # JQ-style scripts use naive Shanghai datetimes; artifacts remain aware.
        return self._host.at.replace(tzinfo=None)

    @property
    def previous_date(self):
        return datetime.fromisoformat(self._host.data.previous[str(self.current_dt.date())]).date()

    @property
    def portfolio(self):
        return self._host.portfolio_view()

    @property
    def run_params(self):
        return SimpleNamespace(
            type="backtest",
            frequency="minute",
            start_date=datetime.fromisoformat(self._host.data.days[0]).date(),
            end_date=datetime.fromisoformat(self._host.data.days[-1]).date(),
        )


class Quote:
    def __init__(self, host, sid):
        self._host, self._sid = host, sid

    @property
    def last_price(self):
        return self._host.data.current(self._sid, self._host.at)

    @property
    def paused(self):
        return self._state()["suspended"]

    def _state(self):
        return self._host.data.state(self._sid, str(self._host.at.date()), at=self._host.at)

    @property
    def high_limit(self):
        return float(self._state()["upper_limit"])

    @property
    def low_limit(self):
        return float(self._state()["lower_limit"])

    @property
    def is_st(self):
        value = self._state().get("is_st")
        require(value is not None, "MINUTE_ST_STATUS_UNKNOWN")
        return value

    @property
    def day_open(self):
        day = str(self._host.at.date())
        row = self._host.data.bars.get((self._sid, self._host.data.ends[day][0]))
        require(
            row is not None and row["_available"] <= self._host.at,
            "MINUTE_DAY_OPEN_NOT_YET_VISIBLE",
        )
        return float(row["open"])

    def __getattr__(self, field):
        raise UnsupportedAPI(f"JQ_CURRENT_DATA_FIELD_UNSUPPORTED:{field}")


class API:
    def __init__(self, host):
        self.host = host
        self.context, self.g = Context(host), SimpleNamespace()
        self.universe = list(host.data.ids)
        self.schedules = []

    def run_daily(self, func, time="open", reference_security=None):
        require(self.host.phase == "initialize", "JQ_SCHEDULE_ONLY_IN_INITIALIZE")
        require(callable(func), "JQ_SCHEDULE_CALLBACK_REQUIRED")
        validate_callback(func)
        require(
            reference_security is None or reference_security in self.host.data.ids,
            "JQ_REFERENCE_SECURITY_OUT_OF_SCOPE",
        )
        aliases = {"open": "09:30", "before_open": "09:00", "after_close": "15:30"}
        clock = aliases.get(time, time)
        supported = {"09:00", "15:30", "every_bar"}
        for day in self.host.data.days:
            valid = supported | {t.strftime("%H:%M") for t in self.host.data.clocks[day]}
            require(clock in valid, f"JQ_SCHEDULE_TIME_UNSUPPORTED:{time}")
        self.schedules.append((func, clock))

    def set_universe(self, security_list):
        self.universe = self.securities(security_list)

    def securities(self, security):
        ids = [security] if isinstance(security, str) else list(security)
        require(
            ids and len(ids) == len(set(ids)) and set(ids) <= set(self.host.data.ids),
            "JQ_SECURITY_OUT_OF_SCOPE",
        )
        return ids

    def set_option(self, name, value):
        require(
            name in {"use_real_price", "avoid_future_data"} and value is True,
            f"JQ_OPTION_UNSUPPORTED:{name}={value}",
        )
        self.host.emit("option", name=name, value=value)

    def set_order_cost(self, cost, type="stock", ref=None):
        c = self.host.config
        expected = OrderCost(
            close_tax=c["sell_tax"],
            open_commission=c["commission_rate"],
            close_commission=c["commission_rate"],
            min_commission=c["min_commission"],
        )
        require(
            type == "stock" and ref is None and cost == expected,
            "JQ_COST_MUST_MATCH_FROZEN_RUN_CONFIG",
        )
        self.host.emit("cost_assertion", cost=vars(cost))

    def set_slippage(self, slippage, type="stock", ref=None):
        require(
            type == "stock" and ref is None and slippage == FixedSlippage(0),
            "JQ_ONLY_ZERO_FIXED_SLIPPAGE",
        )

    def get_current_data(self):
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        return {s: Quote(self.host, s) for s in self.host.data.ids}

    def _history(self, sid, count, unit, fields, *, include_now=True, end=None):
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        self.securities(sid)
        require(unit in {"1m", "minute"}, f"JQ_FREQUENCY_UNSUPPORTED:{unit}")
        require(type(count) is int and 0 < count <= 10000, "JQ_HISTORY_COUNT")
        fields = [fields] if isinstance(fields, str) else list(fields)
        require(
            fields
            and len(set(fields)) == len(fields)
            and set(fields) <= {"open", "high", "low", "close", "volume", "money"},
            "JQ_HISTORY_FIELDS_UNSUPPORTED",
        )
        cutoff = self.host.at if end is None else end
        rows = self.host.data.visible(
            sid, self.host.at, end=cutoff, include_now=include_now, count=count
        )
        require(len(rows) == count, f"JQ_INSUFFICIENT_VISIBLE_HISTORY:{sid}:{count}:{len(rows)}")
        self.host.emit(
            "history_read",
            security=sid,
            count=count,
            cutoff=cutoff.isoformat(),
            knowledge_at=self.host.at.isoformat(),
            include_now=include_now,
            last_end=rows[-1]["bar_end"],
            fields=fields,
        )
        import pandas as pd

        return pd.DataFrame(
            [{f: float(r["amount" if f == "money" else f]) for f in fields} for r in rows],
            index=pd.DatetimeIndex([r["_end"].replace(tzinfo=None) for r in rows]),
        )

    def attribute_history(
        self,
        security,
        count,
        unit="1d",
        fields=("open", "close", "high", "low", "volume", "money"),
        skip_paused=True,
        df=True,
        fq="pre",
    ):
        require(fq is None, "JQ_ADJUSTMENT_UNSUPPORTED:use_fq_None_for_raw_research")
        require(type(skip_paused) is bool and type(df) is bool, "JQ_HISTORY_BOOLEAN_REQUIRED")
        # No artificial bars on suspended dates. skip_paused=False cannot promise
        # JQ forward-filled suspension values, so reject when suspension is present.
        require(
            skip_paused
            or not any(
                self.host.data.states[security, d]["suspended"]
                for d in self.host.data.days
                if d <= str(self.host.at.date())
            ),
            "JQ_PAUSED_FILL_UNSUPPORTED",
        )
        frame = self._history(security, count, unit, fields)
        return frame if df else {f: frame[f].to_numpy() for f in frame}

    def history(
        self,
        count,
        unit="1d",
        field="close",
        security_list=None,
        df=True,
        skip_paused=False,
        fq="pre",
    ):
        require(
            fq is None and type(df) is bool and skip_paused is False,
            "JQ_HISTORY_REQUIRES_RAW_UNFILLED_ALIGNED_INPUT",
        )
        ids = self.securities(self.universe if security_list is None else security_list)
        import pandas as pd

        frames = {s: self._history(s, count, unit, [field]) for s in ids}
        indexes = [f.index for f in frames.values()]
        require(all(indexes[0].equals(i) for i in indexes), "JQ_HISTORY_UNALIGNED_SECURITIES")
        frame = pd.DataFrame({s: f[field] for s, f in frames.items()})
        return frame if df else {s: frame[s].to_numpy() for s in ids}

    def get_bars(
        self,
        security,
        count,
        unit="1d",
        fields=("date", "open", "high", "low", "close"),
        include_now=False,
        end_dt=None,
        fq_ref_date=None,
        df=False,
    ):
        require(
            fq_ref_date is None and type(include_now) is bool and type(df) is bool,
            "JQ_BARS_OPTIONS_UNSUPPORTED",
        )
        wanted = list(fields)
        cols = [f for f in wanted if f != "date"]
        end = self.parse_end(end_dt) if end_dt is not None else None
        frame = self._history(security, count, unit, cols, include_now=include_now, end=end)
        if "date" in wanted:
            frame.insert(0, "date", frame.index)
        frame = frame[wanted]
        return frame if df else frame.to_records(index=False)

    def parse_end(self, value):
        if isinstance(value, datetime):
            value = value.isoformat()
        if isinstance(value, str) and len(value) > 10:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=self.host.at.tzinfo)
            result = stamp(parsed.isoformat())
            require(result <= self.host.at, "JQ_FUTURE_DATA_REQUEST")
            return result
        raise ValueError("JQ_MINUTE_END_REQUIRES_DATETIME")

    def get_price(
        self,
        security,
        start_date=None,
        end_date=None,
        frequency="daily",
        fields=None,
        skip_paused=False,
        fq="pre",
        count=None,
        panel=False,
        fill_paused=False,
    ):
        require(start_date is None and count is not None, "JQ_PRICE_ONLY_COUNT_WINDOW_SUPPORTED")
        require(
            fq is None and skip_paused is False and panel is False and fill_paused is False,
            "JQ_PRICE_REQUIRES_RAW_UNFILLED_PANEL_FALSE",
        )
        ids = self.securities(security)
        end = self.parse_end(end_date) if end_date is not None else None
        fields = fields or ["open", "high", "low", "close", "volume", "money"]
        frames = {s: self._history(s, count, frequency, fields, end=end) for s in ids}
        if isinstance(security, str):
            return frames[security]
        import pandas as pd

        return pd.concat(
            [f.rename_axis("time").reset_index().assign(code=s) for s, f in frames.items()],
            ignore_index=True,
        ).sort_values(["time", "code"])

    def _order(self, api_name, security, value, style, side, pindex, close_today):
        self.securities(security)
        require(
            side == "long" and pindex == 0 and close_today is False,
            "JQ_ONLY_LONG_SINGLE_STOCK_ACCOUNT",
        )
        at, day = self.host.at, str(self.host.at.date())
        require(
            self.host.phase == "bar"
            and at in self.host.data.clocks[day]
            and at < self.host.data.sessions[day][-1][1],
            "JQ_ORDER_OUTSIDE_SESSION",
        )
        require(type(value) in (int, float) and math.isfinite(value), "JQ_ORDER_FINITE_AMOUNT")
        if api_name in {"order_shares", "order_to"}:
            require(float(value).is_integer(), "JQ_ORDER_INTEGER_SHARES")
        if api_name.startswith("order_target") or api_name == "order_to":
            require(value >= 0, "JQ_TARGET_CANNOT_BE_NEGATIVE")
        from rqalpha import api as native
        from rqalpha.model.order import LimitOrder, MarketOrder

        if style is None or isinstance(style, MarketOrderStyle):
            native_style = MarketOrder()
        elif isinstance(style, LimitOrderStyle):
            native_style = LimitOrder(float(style.price))
        else:
            raise UnsupportedAPI("JQ_ORDER_STYLE_UNSUPPORTED")
        self.host.emit(
            "order_request",
            api=api_name,
            security=security,
            value=value,
            style=type(native_style).__name__,
        )
        result = getattr(native, api_name)(security, value, style=native_style)
        if isinstance(result, list):
            require(len(result) <= 1, "JQ_NATIVE_ORDER_RESULT_SCOPE")
            result = result[0] if result else None
        if result is not None:
            self.host.orders[str(result.order_id)] = result
        self.host.emit("order_return", order_id=str(result.order_id) if result else None)
        return order_view(result) if result is not None else None

    def order(self, security, amount, style=None, side="long", pindex=0, close_today=False):
        return self._order("order_shares", security, amount, style, side, pindex, close_today)

    def order_target(self, security, amount, style=None, side="long", pindex=0, close_today=False):
        return self._order("order_to", security, amount, style, side, pindex, close_today)

    def order_value(self, security, value, style=None, side="long", pindex=0, close_today=False):
        return self._order("order_value", security, value, style, side, pindex, close_today)

    def order_target_value(
        self, security, value, style=None, side="long", pindex=0, close_today=False
    ):
        return self._order("order_target_value", security, value, style, side, pindex, close_today)

    def get_orders(self, order_id=None, security=None, status=None):
        result = {i: order_view(o) for i, o in self.host.orders.items()}
        return {
            i: o
            for i, o in result.items()
            if (order_id is None or str(order_id) == i)
            and (security is None or security == o.security)
            and (status is None or status == o.status)
        }

    def get_open_orders(self):
        return {str(o.order_id): order_view(o) for o in self.host.env.broker.get_open_orders()}

    def cancel_order(self, order):
        order_id = str(getattr(order, "order_id", order))
        require(order_id in self.host.orders, "JQ_UNKNOWN_ORDER_ID")
        native = self.host.orders[order_id]
        if native.is_final():
            return False
        self.host.env.broker.cancel_order(native)
        return True

    def get_trades(self):
        return {t["trade_id"]: SimpleNamespace(**t) for t in self.host.trades}

    def record(self, **values):
        require(
            all(type(v) in (int, float) and math.isfinite(v) for v in values.values()),
            "JQ_RECORD_FINITE_NUMBERS_ONLY",
        )
        self.host.emit("record", values=values)

    def namespace(self):
        names = (
            "run_daily set_universe set_option set_order_cost set_slippage get_current_data "
            "attribute_history history get_bars get_price order order_target order_value "
            "order_target_value get_orders get_open_orders cancel_order get_trades record"
        ).split()
        result = {name: getattr(self, name) for name in names}
        for name in (
            "get_fundamentals query valuation indicator income balance cash_flow "
            "get_index_stocks get_industry_stocks get_all_securities get_security_info "
            "get_trade_days get_extras get_factor_values run_weekly run_monthly "
            "set_benchmark set_subportfolios set_data_option read_file write_file "
            "send_message enable_profile"
        ).split():
            result[name] = unsupported(name)
        result.update(
            g=self.g,
            LimitOrderStyle=LimitOrderStyle,
            MarketOrderStyle=MarketOrderStyle,
            OrderCost=OrderCost,
            FixedSlippage=FixedSlippage,
            OrderStatus=OrderStatus,
        )
        result["log"] = SimpleNamespace(
            **{
                level: self.logger(level) for level in ("debug", "info", "warning", "warn", "error")
            },
            set_level=unsupported("log.set_level"),
        )
        module = ModuleType("jqdata")
        module.__dict__.update(result)
        module.__all__ = list(result)

        def missing(name):
            if name.startswith("__"):
                raise AttributeError(name)
            return unsupported(name)()

        module.__getattr__ = missing
        sys.modules["jqdata"] = module
        return result

    def logger(self, level):
        def write(*values):
            self.host.emit("log", level=level, message=" ".join(map(str, values)))

        return write
