"""Daily history and schedule adapters; orders/logs/context share the Python SDK."""

from datetime import date, datetime

from ashare_quant.backtests.minute.api import API, Quote, validate_callback
from ashare_quant.backtests.minute.inputs import require, stamp


class DailyQuote(Quote):
    @property
    def day_open(self):
        require(self._host.at.strftime("%H:%M") >= "09:30", "JQ_DAY_OPEN_NOT_VISIBLE")
        return float(self._host.data.row(self._sid, self._host.day)["open"])


class DailyAPI(API):
    def run_daily(self, func, time="open", reference_security=None):
        require(self.host.phase == "initialize", "JQ_SCHEDULE_ONLY_IN_INITIALIZE")
        validate_callback(func)
        require(
            reference_security is None or reference_security in self.host.data.ids,
            "JQ_REFERENCE_SECURITY_OUT_OF_SCOPE",
        )
        clock = {"open": "09:30", "before_open": "09:00", "after_close": "15:30"}.get(time, time)
        require(
            clock in {"09:00", "09:30", "15:30", "every_bar"},
            f"JQ_DAILY_SCHEDULE_UNSUPPORTED:{time}",
        )
        self.schedules.append((func, clock))

    def validate_order_clock(self, security):
        require(
            self.host.phase == "bar" and self.host.at.strftime("%H:%M") == "09:30",
            "JQ_DAILY_ORDERS_AT_OPEN_ONLY",
        )

    def get_current_data(self):
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        return {s: DailyQuote(self.host, s) for s in self.host.data.ids}

    def parse_end(self, value):
        if isinstance(value, datetime):
            value = value.isoformat()
        if isinstance(value, date):
            value = value.isoformat()
        if isinstance(value, str) and len(value) == 10:
            day = date.fromisoformat(value)
            require(day <= self.host.at.date(), "JQ_FUTURE_DATA_REQUEST")
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
        require(self.host.phase != "initialize", "JQ_DATA_UNAVAILABLE_IN_INITIALIZE")
        self.securities(sid)
        require(unit in {"daily", "1d"}, f"JQ_FREQUENCY_UNSUPPORTED:{unit}")
        require(count is None or (type(count) is int and 0 < count <= 10000), "JQ_HISTORY_COUNT")
        fields = [fields] if isinstance(fields, str) else list(fields)
        require(
            fields
            and len(set(fields)) == len(fields)
            and set(fields) <= {"open", "high", "low", "close", "volume", "money"},
            "JQ_HISTORY_FIELDS_UNSUPPORTED",
        )
        rows = self.host.data.visible(sid, self.host.at, end=end, include_now=include_now)
        if start is not None:
            rows = [r for r in rows if r["date"] >= start]
        if skip_paused:
            rows = [r for r in rows if r["tradestatus"] == "1"]
        if count is not None:
            rows = rows[-count:]
        require(
            rows and (count is None or len(rows) == count),
            f"JQ_INSUFFICIENT_VISIBLE_HISTORY:{sid}:{count}:{len(rows)}; increase warmup_sessions",
        )
        latest = rows[-1]["date"]
        if basis_date is None:
            basis_date = latest
        require(latest <= basis_date <= str(self.host.at.date()), "JQ_FUTURE_DATA_REQUEST")
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
        require(type(skip_paused) is bool and type(df) is bool, "JQ_HISTORY_BOOLEAN_REQUIRED")
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
        require(type(df) is bool and type(skip_paused) is bool, "JQ_HISTORY_BOOLEAN_REQUIRED")
        ids = self.securities(self.universe if security_list is None else security_list)
        import pandas as pd

        frames = {
            s: self._history(s, count, unit, [field], skip_paused=skip_paused, adjustment=fq)
            for s in ids
        }
        indexes = [f.index for f in frames.values()]
        require(all(indexes[0].equals(i) for i in indexes), "JQ_HISTORY_UNALIGNED_SECURITIES")
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
        require(type(include_now) is bool and type(df) is bool, "JQ_BARS_OPTIONS_UNSUPPORTED")
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
        require(
            type(skip_paused) is bool and panel is False and fill_paused is False,
            "JQ_PRICE_REQUIRES_UNFILLED_PANEL_FALSE",
        )
        require(count is None or start_date is None, "JQ_DAILY_COUNT_OR_START_REQUIRED")
        start = str(self.parse_end(start_date).date()) if start_date is not None else None
        end = self.parse_end(end_date) if end_date is not None else None
        require(start is None or start <= str((end or self.host.at).date()), "JQ_DAILY_DATE_ORDER")
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
            for s in self.securities(security)
        }
        if isinstance(security, str):
            return frames[security]
        import pandas as pd

        return pd.concat(
            [f.rename_axis("time").reset_index().assign(code=s) for s, f in frames.items()],
            ignore_index=True,
        ).sort_values(["time", "code"])
