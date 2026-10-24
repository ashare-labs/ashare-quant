"""Daily history and schedule adapters; orders/logs/context share the Python SDK."""

from datetime import date, datetime

from ashare_quant.backtests.minute.api import (
    API,
    CurrentQuotes,
    Quote,
    validate_callback,
    validate_reference_calendar,
)
from ashare_quant.backtests.minute.inputs import require, stamp


class DailyQuote(Quote):
    @property
    def day_open(self):
        require(self._host.at.strftime("%H:%M") >= "09:30", "AQUANT_DAY_OPEN_NOT_VISIBLE")
        if getattr(self._host.data, "demand", False):
            return self._host.data.observed_value(self._sid, self._host.day, "open")
        return float(self._host.data.row(self._sid, self._host.day)["open"])


class DailyAPI(API):
    def run_daily(self, func, time="open", reference_security=None):
        require(self.host.phase == "initialize", "AQUANT_SCHEDULE_ONLY_IN_INITIALIZE")
        validate_callback(func)
        validate_reference_calendar(reference_security)
        clock = {"open": "09:30", "before_open": "09:00", "after_close": "15:30"}.get(time, time)
        require(
            clock in {"09:00", "09:30", "15:30", "every_bar"},
            f"AQUANT_DAILY_SCHEDULE_UNSUPPORTED:{time}",
        )
        self.schedules.append((func, clock))

    def validate_order_clock(self, security):
        require(
            self.host.phase == "bar" and self.host.at.strftime("%H:%M") == "09:30",
            "AQUANT_DAILY_ORDERS_AT_OPEN_ONLY",
        )

    def get_current_data(self):
        require(self.host.phase != "initialize", "AQUANT_DATA_UNAVAILABLE_IN_INITIALIZE")
        if getattr(self.host.data, "demand", False):
            return CurrentQuotes(self.host, DailyQuote)
        return {s: DailyQuote(self.host, s) for s in self.host.data.ids}

    def parse_end(self, value):
        if isinstance(value, datetime):
            value = value.isoformat()
        if isinstance(value, date):
            value = value.isoformat()
        if isinstance(value, str) and len(value) == 10:
            day = date.fromisoformat(value)
            require(day <= self.host.at.date(), "AQUANT_FUTURE_DATA_REQUEST")
            return min(stamp(value + "T15:00:00+08:00"), self.host.at)
        return super().parse_end(value)

    def _history(
        self,
        sid,
        count,
        unit,
        fields,
        *,
        include_now=True,
        end=None,
        start=None,
        skip_paused=False,
        adjustment=None,
        basis_date=None,
    ):
        require(self.host.phase != "initialize", "AQUANT_DATA_UNAVAILABLE_IN_INITIALIZE")
        self.securities(sid)
        require(unit in {"daily", "1d"}, f"AQUANT_FREQUENCY_UNSUPPORTED:{unit}")
        require(count is None or (type(count) is int and 0 < count), "AQUANT_HISTORY_COUNT")
        if not getattr(self.host.data, "demand", False):
            require(count is None or count <= 10000, "AQUANT_HISTORY_COUNT")
        fields = [fields] if isinstance(fields, str) else list(fields)
        require(
            fields
            and len(set(fields)) == len(fields)
            and set(fields) <= {"open", "high", "low", "close", "volume", "money"},
            "AQUANT_HISTORY_FIELDS_UNSUPPORTED",
        )
        demand = getattr(self.host.data, "demand", False)
        rows = (self.host.data.daily_history(
            sid, self.host.at, count=count, start=start, end=end, include_now=include_now,
            skip_paused=skip_paused, fields=fields,
        ) if demand else self.host.data.visible(sid, self.host.at, end=end, include_now=include_now))
        if start is not None and not demand:
            require(start >= self.host.data.calendar[0], "AQUANT_HISTORY_RANGE_OUTSIDE_FROZEN_INPUT")
            rows = [r for r in rows if r["date"] >= start]
        if skip_paused and not demand:
            selected = []
            for row in reversed(rows):
                if not self.host.data.is_paused(sid, row["date"]):
                    selected.append(row)
                if count is not None and len(selected) == count:
                    break
            rows = list(reversed(selected))
        elif count is not None and not demand:
            rows = rows[-count:]
        require(
            rows and (count is None or len(rows) == count),
            f"AQUANT_INSUFFICIENT_VISIBLE_HISTORY:{sid}:{count}:{len(rows)}",
        )
        latest = rows[-1]["date"]
        if basis_date is None:
            basis_date = latest
        require(latest <= basis_date <= str(self.host.at.date()), "AQUANT_FUTURE_DATA_REQUEST")
        response = self.host.data.history_prices(rows, adjustment, basis_date)
        rows = response["rows"]
        self.host.emit(
            "daily_history_read",
            security=sid,
            count=count,
            fields=fields,
            knowledge_at=self.host.at.isoformat(),
            dates=[r["date"] for r in rows],
            price_basis=response["price_basis"],
            adjustment=response.get("adjustment"),
            receipt_sha256=response["receipt"].get("receipt_sha256"),
            include_now=include_now,
            skip_paused=skip_paused,
        )
        import pandas as pd

        frame = pd.DataFrame(
            [{f: float(r["amount" if f == "money" else f]) for f in fields} for r in rows],
            index=pd.DatetimeIndex([r["date"] for r in rows]),
        )
        frame.attrs["unavailable_minutes"] = []  # Shared tabular projection compatibility.
        frame.attrs["price_basis"] = response["price_basis"]
        frame.attrs["receipt"] = response["receipt"]
        frame.attrs["adjustment"] = response.get("adjustment")
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
        if getattr(self.host.data, "demand", False) and unit in {"minute", "1m"}:
            return API(self.host).attribute_history(
                security, count, unit, fields, skip_paused, df, fq
            )
        require(type(skip_paused) is bool and type(df) is bool, "AQUANT_HISTORY_BOOLEAN_REQUIRED")
        frame = self._history(security, count, unit, fields, skip_paused=skip_paused, adjustment=fq)
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
        if getattr(self.host.data, "demand", False) and unit in {"minute", "1m"}:
            return API(self.host).history(
                count, unit, field, self.universe if security_list is None else security_list,
                df, skip_paused, fq,
            )
        require(type(df) is bool and type(skip_paused) is bool, "AQUANT_HISTORY_BOOLEAN_REQUIRED")
        ids = self.securities(self.universe if security_list is None else security_list)
        if getattr(self.host.data, "demand", False):
            require(type(count) is int and count > 0, "AQUANT_HISTORY_COUNT")
            self.host.data.check_budget(len(ids) * count)
        import pandas as pd

        frames = {
            s: self._history(s, count, unit, [field], skip_paused=skip_paused, adjustment=fq)
            for s in ids
        }
        indexes = [f.index for f in frames.values()]
        require(all(indexes[0].equals(i) for i in indexes), "AQUANT_HISTORY_UNALIGNED_SECURITIES")
        frame = pd.DataFrame({s: f[field] for s, f in frames.items()})
        frame.attrs["receipts"] = {s: f.attrs["receipt"] for s, f in frames.items()}
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
        if getattr(self.host.data, "demand", False) and unit in {"minute", "1m"}:
            return API(self.host).get_bars(
                security, count, unit, fields, include_now, end_dt, fq_ref_date, df
            )
        require(type(include_now) is bool and type(df) is bool, "AQUANT_BARS_OPTIONS_UNSUPPORTED")
        wanted = list(fields)
        cols = [f for f in wanted if f != "date"]
        end = self.parse_end(end_dt) if end_dt is not None else None
        basis = str(self.parse_end(fq_ref_date).date()) if fq_ref_date is not None else None
        frame = self._history(
            security,
            count,
            unit,
            cols,
            include_now=include_now,
            end=end,
            adjustment="pre" if basis else None,
            basis_date=basis,
        )
        if "date" in wanted:
            frame.insert(0, "date", frame.index)
        frame = frame[wanted]
        return frame if df else frame.to_records(index=False)

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
        if getattr(self.host.data, "demand", False) and frequency in {"minute", "1m"}:
            return API(self.host).get_price(
                security, start_date, end_date, frequency, fields, skip_paused,
                fq, count, panel, fill_paused,
            )
        require(
            type(skip_paused) is bool and panel is False and fill_paused is False,
            "AQUANT_PRICE_REQUIRES_UNFILLED_PANEL_FALSE",
        )
        require(count is None or start_date is None, "AQUANT_DAILY_COUNT_OR_START_REQUIRED")
        start = str(self.parse_end(start_date).date()) if start_date is not None else None
        end = self.parse_end(end_date) if end_date is not None else None
        require(start is None or start <= str((end or self.host.at).date()), "AQUANT_DAILY_DATE_ORDER")
        ids = self.securities(security)
        if getattr(self.host.data, "demand", False):
            size = count if type(count) is int else sum(
                (start is None or d >= start) and stamp(d + "T15:00:00+08:00") <= (end or self.host.at)
                for d in self.host.data.calendar
            )
            self.host.data.check_budget(len(ids) * size)
        frames = {
            s: self._history(
                s,
                count,
                frequency,
                fields or ["open", "high", "low", "close", "volume", "money"],
                end=end,
                start=start,
                skip_paused=skip_paused,
                adjustment=fq,
            )
            for s in ids
        }
        if isinstance(security, str):
            return frames[security]
        import pandas as pd

        return pd.concat(
            [f.rename_axis("time").reset_index().assign(code=s) for s, f in frames.items()],
            ignore_index=True,
        ).sort_values(["time", "code"])
