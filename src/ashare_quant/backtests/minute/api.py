"""Explicit JQ-style subset; functions delegate orders to native RQAlpha APIs."""

import inspect
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import ModuleType, SimpleNamespace

from ashare_quant.backtests.minute.inputs import number, require, stamp


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


def order_view(order, *, jq=False, holding_cost=None):
    statuses = {
        "PENDING_NEW": "new",
        "ACTIVE": "held",
        "FILLED": "filled",
        "CANCELLED": "canceled",
        "REJECTED": "rejected",
    }
    status = statuses[order.status.name]
    if jq:
        if status == "held" and order.filled_quantity:
            status = "filled"  # Observed partial limit label, still in get_open_orders.
        elif status == "filled":
            status = "held"  # Observed JQ terminal full-fill label, no longer open.
    view = SimpleNamespace(
        order_id=str(order.order_id),
        security=order.order_book_id,
        amount=int(order.quantity),
        filled=int(order.filled_quantity),
        is_buy=order.side.name == "BUY",
        status=OrderStatus(status),
        price=float(order.price),
        avg_cost=float(order.avg_price),
        add_time=order.datetime,
        native_status=order.status.name,
        message=order.message,
    )
    if jq:
        view.commission = float(order.transaction_cost)
        view.limit_price = float(order.price)
        view.price = round(float(order.avg_price), 3) if order.filled_quantity else 0.0
        view.avg_cost = (
            float(holding_cost) if not view.is_buy and holding_cost is not None else view.price
        )
    return view


class Context:
    def __init__(self, host):
        self._host = host

    @property
    def current_dt(self):
        # JQ-style scripts use naive Shanghai datetimes; artifacts remain aware.
        return self._host.at.replace(tzinfo=None)

    @property
    def parameters(self):
        from copy import deepcopy
        return deepcopy(self._host.parameters)

    @property
    def securities(self):
        return tuple(self._host.api.universe)

    @property
    def previous_date(self):
        return datetime.fromisoformat(self._host.data.previous[str(self.current_dt.date())]).date()

    @property
    def portfolio(self):
        return self._host.portfolio_view()

    @property
    def run_params(self):
        return SimpleNamespace(
            type="full_backtest" if self._host.jq_lifecycle else "backtest",
            frequency="daily" if getattr(self._host.data, "frequency", "1m") == "1d" else "minute",
            start_date=datetime.fromisoformat(self._host.data.days[0]).date(),
            end_date=datetime.fromisoformat(self._host.data.days[-1]).date(),
        )


class Quote:
    def __init__(self, host, sid):
        self._host, self._sid = host, sid

    @property
    def last_price(self):
        if getattr(self._host.data, "demand", False):
            return self._host.data.quote_price(self._sid, self._host.at)
        return self._host.data.current(self._sid, self._host.at)

    @property
    def valuation(self):
        require(self._host.data.generic_research, "MINUTE_SOURCE_QUALITY_PROFILE_REQUIRED")
        return self._host.data.valuation_mark(self._sid, self._host.at)

    @property
    def paused(self):
        if getattr(self._host.data, "demand", False):
            require(self._host.at.strftime("%H:%M") >= "09:00", "PYTHON_DAILY_STATE_NOT_VISIBLE")
            return self._host.data.is_paused(self._sid, self._host.day)
        return self._state()["suspended"]

    def _state(self):
        return self._host.data.state(self._sid, str(self._host.at.date()), at=self._host.at)

    @property
    def high_limit(self):
        require(not self._host.data.research, "MINUTE_EXCHANGE_PRICE_LIMIT_UNKNOWN")
        return float(self._state()["upper_limit"])

    @property
    def low_limit(self):
        require(not self._host.data.research, "MINUTE_EXCHANGE_PRICE_LIMIT_UNKNOWN")
        return float(self._state()["lower_limit"])

    @property
    def is_st(self):
        if getattr(self._host.data, "demand", False):
            require(self._host.at.strftime("%H:%M") >= "09:00", "PYTHON_DAILY_STATE_NOT_VISIBLE")
            value = self._host.data._row(self._sid, self._host.day).get("is_st")
            require(value in {"0", "1"}, "MINUTE_ST_STATUS_UNKNOWN")
            return value == "1"
        value = self._state().get("is_st")
        require(value is not None, "MINUTE_ST_STATUS_UNKNOWN")
        return value

    @property
    def day_open(self):
        if self._host.data.session_data is not None:
            return self._host.data.session_data.visible_open(self._sid, self._host.at)
        require(not self._host.data.research, "MINUTE_DAY_OPEN_OUTSIDE_RESEARCH_SESSION")
        day = str(self._host.at.date())
        row = self._host.data.bars.get((self._sid, self._host.data.ends[day][0]))
        require(
            row is not None and row["_available"] <= self._host.at,
            "MINUTE_DAY_OPEN_NOT_YET_VISIBLE",
        )
        return float(row["open"])

    def __getattr__(self, field):
        raise UnsupportedAPI(f"JQ_CURRENT_DATA_FIELD_UNSUPPORTED:{field}")


class CurrentQuotes(Mapping):
    """Keys refer to the frozen catalogue; values are read only when requested."""

    def __init__(self, host, quote=Quote):
        self.host, self.quote = host, quote

    def __getitem__(self, sid):
        self.host.data.securities([sid])
        return self.quote(self.host, sid)

    def __iter__(self):
        return iter(self.host.data.owner.release.identifiers)

    def __len__(self):
        return len(self.host.data.owner.release.identifiers)


class API:
    def __init__(self, host):
        self.host = host
        self.context, self.g = Context(host), SimpleNamespace()
        self.universe = list(host.data.ids)
        self.schedules = []

    def _order_view(self, order):
        return order_view(
            order,
            jq=self.host.jq_lifecycle,
            holding_cost=self.host.order_origin_cost.get(str(order.order_id)),
        )

    def run_daily(self, func, time="open", reference_security=None):
        require(self.host.phase == "initialize", "JQ_SCHEDULE_ONLY_IN_INITIALIZE")
        require(callable(func), "JQ_SCHEDULE_CALLBACK_REQUIRED")
        validate_callback(func)
        require(
            reference_security is None or self.securities(reference_security),
            "JQ_REFERENCE_SECURITY_OUT_OF_SCOPE",
        )
        aliases = {"open": "09:30", "before_open": "09:00", "after_close": "15:30"}
        clock = aliases.get(time, time)
        if self.host.jq_lifecycle:
            from ashare_quant.backtests.minute.lifecycle import schedule_clock

            require(
                isinstance(clock, str) and schedule_clock(clock),
                f"JQ_SCHEDULE_TIME_UNSUPPORTED:{time}",
            )
            self.schedules.append((func, clock))
            return
        supported = {"09:00", "15:30", "every_bar"}
        for day in self.host.data.days:
            valid = supported | {t.strftime("%H:%M") for t in self.host.data.clocks[day]}
            require(clock in valid, f"JQ_SCHEDULE_TIME_UNSUPPORTED:{time}")
        self.schedules.append((func, clock))

    def set_universe(self, security_list):
        if getattr(self.host.data, "demand", False):
            ids = self.host.data.securities(list(security_list) if not isinstance(security_list, str) else [security_list])
            self.host.data.register(ids)
            from rqalpha.api import update_universe
            update_universe(ids)
            self.universe = ids
            self.host.emit("universe_changed", securities=ids)
        else:
            self.universe = self.securities(security_list)

    def securities(self, security):
        ids = [security] if isinstance(security, str) else list(security)
        if getattr(self.host.data, "demand", False):
            require(ids, "JQ_SECURITIES_REQUIRED")
            return self.host.data.securities(ids)
        require(
            ids and len(ids) == len(set(ids)) and set(ids) <= set(self.host.data.ids),
            "JQ_SECURITY_OUT_OF_SCOPE",
        )
        return ids

    def get_all_securities(self, types=None, date=None):
        require(getattr(self.host.data, "demand", False), "JQ_DIRECTORY_UNAVAILABLE")
        require(types is None or types == [] or types == ["stock"], "JQ_DIRECTORY_TYPES_UNSUPPORTED")
        as_of = str(self.host.at.date()) if date is None else str(date)[:10]
        from datetime import date as Date
        require(str(Date.fromisoformat(as_of)) == as_of and as_of <= str(self.host.at.date()),
                "JQ_FUTURE_DATA_REQUEST")
        rows, receipt = self.host.data.catalogue(as_of)
        import pandas as pd
        frame = pd.DataFrame([
            {"code": r["security"], "display_name": r.get("name"), "start_date": r["start_date"],
             "name_is_historical": False} for r in rows
        ], columns=["code", "display_name", "start_date", "name_is_historical"]).set_index("code")
        frame.attrs.update(receipt=receipt, historical_pit_verified=False)
        return frame

    def set_option(self, name, value):
        if name == "order_volume_ratio":
            require(
                self.host.phase == "initialize"
                and self.host.jq_capacity
                and type(value) in (int, float)
                and number(value) == number(self.host.config["volume_percent"]),
                "JQ_VOLUME_RATIO_MUST_MATCH_EXPLICIT_CAPACITY_POLICY",
            )
            self.host.emit("option", name=name, value=value)
            return
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
        if getattr(self.host.data, "demand", False):
            return CurrentQuotes(self.host)
        return {s: Quote(self.host, s) for s in self.host.data.ids}

    def _history(self, sid, count, unit, fields, *, include_now=True, end=None, start=None):
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        self.securities(sid)
        require(unit in {"1m", "minute"}, f"JQ_FREQUENCY_UNSUPPORTED:{unit}")
        demand = getattr(self.host.data, "demand", False)
        require((demand and count is None and start is not None) or (type(count) is int and count > 0),
                "JQ_HISTORY_COUNT")
        if demand:
            if count is not None:
                self.host.data.check_budget(count)
        else:
            require(count <= 10000, "JQ_HISTORY_COUNT")
        fields = [fields] if isinstance(fields, str) else list(fields)
        require(
            fields
            and len(set(fields)) == len(fields)
            and set(fields) <= {"open", "high", "low", "close", "volume", "money"},
            "JQ_HISTORY_FIELDS_UNSUPPORTED",
        )
        cutoff = self.host.at if end is None else end
        if demand:
            rows = self.host.data.minute_history(
                sid, self.host.at, count=count, start=start, end=cutoff,
                include_now=include_now, fields=fields,
            )
        else:
            rows = self.host.data.visible(sid, self.host.at, end=cutoff,
                                          include_now=include_now, count=count, preserve_missing=True)
        require(count is None or len(rows) == count, f"JQ_INSUFFICIENT_VISIBLE_HISTORY:{sid}:{count}:{len(rows)}")
        unavailable = [
            {"security": sid, "source_label": r["source_label"], "reason": r["missing_reason"]}
            for r in rows
            if r.get("missing_reason")
        ]
        self.host.emit(
            "history_read",
            security=sid,
            count=count,
            cutoff=cutoff.isoformat(),
            knowledge_at=self.host.at.isoformat(),
            include_now=include_now,
            last_end=rows[-1]["bar_end"],
            fields=fields,
            unavailable_minutes=unavailable,
        )
        import pandas as pd

        frame = pd.DataFrame(
            [
                {
                    f: float(r["amount" if f == "money" else f])
                    if not r.get("missing_reason")
                    else float("nan")
                    for f in fields
                }
                for r in rows
            ],
            index=pd.DatetimeIndex([r["_end"].replace(tzinfo=None) for r in rows]),
        )
        frame.attrs["unavailable_minutes"] = unavailable
        return frame

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
        if getattr(self.host.data, "demand", False) and unit in {"daily", "1d"}:
            from ashare_quant.backtests.daily.python_api import DailyAPI
            return DailyAPI(self.host).attribute_history(
                security, count, unit, fields, skip_paused, df, fq
            )
        require(fq is None, "JQ_ADJUSTMENT_UNSUPPORTED:use_fq_None_for_raw_research")
        require(type(skip_paused) is bool and type(df) is bool, "JQ_HISTORY_BOOLEAN_REQUIRED")
        # No artificial bars on suspended dates. skip_paused=False cannot promise
        # JQ forward-filled suspension values, so reject when suspension is present.
        require(
            getattr(self.host.data, "demand", False) or skip_paused
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
        if getattr(self.host.data, "demand", False) and unit in {"daily", "1d"}:
            from ashare_quant.backtests.daily.python_api import DailyAPI
            return DailyAPI(self.host).history(
                count, unit, field, self.universe if security_list is None else security_list,
                df, skip_paused, fq,
            )
        require(
            fq is None and type(df) is bool and skip_paused is False,
            "JQ_HISTORY_REQUIRES_RAW_UNFILLED_ALIGNED_INPUT",
        )
        ids = self.securities(self.universe if security_list is None else security_list)
        if getattr(self.host.data, "demand", False):
            require(type(count) is int and count > 0, "JQ_HISTORY_COUNT")
            self.host.data.check_budget(len(ids) * count)
        import pandas as pd

        frames = {s: self._history(s, count, unit, [field]) for s in ids}
        indexes = [f.index for f in frames.values()]
        require(all(indexes[0].equals(i) for i in indexes), "JQ_HISTORY_UNALIGNED_SECURITIES")
        frame = pd.DataFrame({s: f[field] for s, f in frames.items()})
        frame.attrs["unavailable_minutes"] = [
            gap for f in frames.values() for gap in f.attrs["unavailable_minutes"]
        ]
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
        if getattr(self.host.data, "demand", False) and unit in {"daily", "1d"}:
            from ashare_quant.backtests.daily.python_api import DailyAPI
            return DailyAPI(self.host).get_bars(
                security, count, unit, fields, include_now, end_dt, fq_ref_date, df
            )
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
        demand = getattr(self.host.data, "demand", False)
        if demand and frequency in {"daily", "1d"}:
            from ashare_quant.backtests.daily.python_api import DailyAPI
            return DailyAPI(self.host).get_price(
                security, start_date, end_date, frequency, fields, skip_paused,
                fq, count, panel, fill_paused,
            )
        if frequency in {"daily", "1d"} and self.host.jq_lifecycle:
            require(self.host.data.session_data is not None, "JQ_DAILY_REQUIRES_AD_OBSERVATIONS")
            require(
                fq is None and skip_paused is False and panel is False and fill_paused is False,
                "JQ_PRICE_REQUIRES_RAW_UNFILLED_PANEL_FALSE",
            )
            return self._daily_price(security, start_date, end_date, fields, count)
        require((demand and start_date is not None and count is None)
                or (start_date is None and count is not None), "JQ_PRICE_COUNT_OR_START_REQUIRED")
        require(
            fq is None and skip_paused is False and panel is False and fill_paused is False,
            "JQ_PRICE_REQUIRES_RAW_UNFILLED_PANEL_FALSE",
        )
        ids = self.securities(security)
        def boundary(value, start=False):
            if value is None:
                return None
            if demand and len(str(value)) == 10:
                day = str(value)
                require(day <= str(self.host.at.date()), "JQ_FUTURE_DATA_REQUEST")
                return min(stamp(day + ('T00:00:00+08:00' if start else 'T15:00:00+08:00')), self.host.at)
            return self.parse_end(value)
        end, start = boundary(end_date), boundary(start_date, True)
        require(start is None or start <= (end or self.host.at), "JQ_PRICE_DATE_ORDER")
        fields = fields or ["open", "high", "low", "close", "volume", "money"]
        if demand and type(count) is int:
            self.host.data.check_budget(len(ids)*count)
        frames = {s: self._history(s, count, frequency, fields, end=end, start=start) for s in ids}
        if isinstance(security, str):
            return frames[security]
        import pandas as pd

        frame = pd.concat(
            [f.rename_axis("time").reset_index().assign(code=s) for s, f in frames.items()],
            ignore_index=True,
        ).sort_values(["time", "code"])
        frame.attrs["unavailable_minutes"] = [
            gap for f in frames.values() for gap in f.attrs["unavailable_minutes"]
        ]
        return frame

    def _daily_price(self, security, start, end, fields, count):
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        ids = self.securities(security)
        fields = [fields] if isinstance(fields, str) else list(fields or ["close", "volume"])
        require(
            fields and len(set(fields)) == len(fields) and set(fields) <= {"close", "volume"},
            "JQ_DAILY_FIELDS_UNSUPPORTED",
        )
        require(
            count is None or (type(count) is int and 0 < count <= 10000 and start is None),
            "JQ_DAILY_COUNT_OR_START_REQUIRED",
        )

        def day(value, default):
            if value is None:
                return default
            if isinstance(value, datetime):
                return str(self.parse_end(value).date())
            if isinstance(value, date):
                return value.isoformat()
            require(isinstance(value, str) and len(value) == 10, "JQ_DAILY_DATE_REQUIRED")
            return date.fromisoformat(value).isoformat()

        start = day(start, self.host.data.calendar[0])
        end = day(end, str(self.host.at.date()))
        import pandas as pd

        frames = {}
        for sid in ids:
            rows = self.host.data.session_data.visible_daily(sid, self.host.at, start, end)
            if count is not None:
                rows = rows[-count:]
            require(
                rows and (count is None or len(rows) == count), "JQ_INSUFFICIENT_VISIBLE_DAILY_DATA"
            )
            self.host.emit(
                "daily_history_read",
                security=sid,
                start=start,
                end=end,
                knowledge_at=self.host.at.isoformat(),
                fields=fields,
                dates=[r["date"] for r in rows],
            )
            frames[sid] = pd.DataFrame(
                [{f: float(r[f]) for f in fields} for r in rows],
                index=pd.DatetimeIndex([r["date"] for r in rows]),
            )
        if isinstance(security, str):
            return frames[security]
        return pd.concat(
            [f.rename_axis("time").reset_index().assign(code=s) for s, f in frames.items()],
            ignore_index=True,
        ).sort_values(["time", "code"])

    def validate_order_clock(self, security):
        at, day = self.host.at, str(self.host.at.date())
        if self.host.jq_lifecycle:
            clock = at.strftime("%H:%M")
            require(
                self.host.phase in {"scheduled", "bar"}
                and ("09:30" <= clock < "11:30" or "13:00" <= clock < "15:00"),
                "JQ_ORDER_OUTSIDE_SESSION",
            )
        else:
            require(
                self.host.phase == "bar"
                and at in self.host.data.clocks[day]
                and at < self.host.data.sessions[day][-1][1],
                "JQ_ORDER_OUTSIDE_SESSION",
            )
        if self.host.config["matching"] == "jq_current":
            row = self.host.data.bars.get((security, at))
            if self.host.jq_capacity and row is None and at.strftime("%H:%M") in {"09:30", "13:00"}:
                self.host.data.boundary_execution_price(security, at)
            else:
                require(
                    row is not None and row["_available"] <= at,
                    "JQ_CURRENT_REQUIRES_VISIBLE_CLOSED_BAR:opening_or_missing_bar_not_supported",
                )

    def _order(self, api_name, security, value, style, side, pindex, close_today):
        self.securities(security)
        require(
            side == "long" and pindex == 0 and close_today is False,
            "JQ_ONLY_LONG_SINGLE_STOCK_ACCOUNT",
        )
        self.validate_order_clock(security)
        if getattr(self.host.data, "demand", False):
            self.host.data.component(security, str(self.host.at.date()))
        at = self.host.at
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
            self.host.data.check_order_price(security, style.price, at=at)
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
        return self._order_view(result) if result is not None else None

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
        # The two frozen JQ runs expose no current-session collection from the
        # terminal callback. Keep the native ledger and pending orders intact.
        if self.host.jq_lifecycle and self.host.phase == "end":
            return {}
        result = {
            i: self._order_view(o)
            for i, o in self.host.orders.items()
            if not self.host.jq_lifecycle or o.datetime.date() == self.host.at.date()
        }
        return {
            i: o
            for i, o in result.items()
            if (order_id is None or str(order_id) == i)
            and (security is None or security == o.security)
            and (status is None or status == o.status)
        }

    def get_open_orders(self):
        return {
            str(o.order_id): self._order_view(o) for o in self.host.env.broker.get_open_orders()
        }

    def cancel_order(self, order):
        order_id = str(getattr(order, "order_id", order))
        require(order_id in self.host.orders, "JQ_UNKNOWN_ORDER_ID")
        native = self.host.orders[order_id]
        if native.is_final():
            return False
        self.host.env.broker.cancel_order(native)
        return True

    def get_trades(self):
        if self.host.jq_lifecycle and self.host.phase == "end":
            return {}
        return {
            t["trade_id"]: SimpleNamespace(
                **t,
                **(
                    {"time": datetime.fromisoformat(t["at"]).replace(tzinfo=None)}
                    if self.host.jq_lifecycle
                    else {}
                ),
            )
            for t in self.host.trades
            if not self.host.jq_lifecycle or t["at"][:10] == str(self.host.at.date())
        }

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
            "order_target_value get_orders get_open_orders cancel_order get_trades record get_all_securities"
        ).split()
        result = {name: getattr(self, name) for name in names}
        for name in (
            "get_fundamentals query valuation indicator income balance cash_flow "
            "get_index_stocks get_industry_stocks get_security_info "
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
