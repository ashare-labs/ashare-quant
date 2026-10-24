"""Consumer checks for AD-supplied history, opening, close and daily observations.

No source acquisition or OHLC construction lives here. A valuation observation
cannot create a matching bar. Research replay clocks remain distinct from the
source's unknown historical availability.
"""

from datetime import timedelta

from ashare_quant.backtests.minute.inputs import number, require, stamp

SCHEMA = "ad-minute-session-observations.v1"


class SessionData:
    def __init__(self, data, document):
        require(document.get("schema") == SCHEMA, "MINUTE_SESSION_OBSERVATION_SCHEMA")
        require(
            document.get("source_refs") and document.get("limitations") is not None,
            "MINUTE_SESSION_SOURCE_EVIDENCE",
        )
        self.data = data
        self.document = document
        self.opening = self._read(document["opening_prices"], "opening_price", ("price",))
        self.closing = self._read(document["closing_marks"], "valuation_only", ("price",))
        self.daily = self._read(document["daily"], "closed_daily", ("close", "volume"))
        expected = {(s, d) for s in data.ids for d in data.days}
        require(
            set(self.opening) == set(self.closing) == expected,
            "MINUTE_SESSION_OPENING_CLOSE_COVERAGE",
        )
        require(expected <= set(self.daily), "MINUTE_SESSION_DAILY_COVERAGE")
        self.history = self._history(document["history_minutes"], document["history_sessions"])

    def _available(self, row):
        require(row.get("evidence") and row.get("source_label"), "MINUTE_SESSION_ROW_EVIDENCE")
        require(
            stamp(row["source_label"]).date().isoformat() == row["date"],
            "MINUTE_SESSION_SOURCE_DATE",
        )
        stamp(row["observed_at"])
        if self.data.visibility == "assumed_closed":
            require(
                "available_at" in row
                and row["available_at"] is None
                and "historical_available_at" in row
                and row["historical_available_at"] is None,
                "MINUTE_SESSION_PRESERVE_UNKNOWN_AVAILABILITY",
            )
            return stamp(row["replay_available_at"])
        require("replay_available_at" not in row, "MINUTE_SESSION_UNDECLARED_REPLAY")
        return stamp(row["available_at"])

    def _read(self, rows, purpose, fields):
        result = {}
        for row in rows:
            key = row["security"], row["date"]
            require(
                key not in result
                and key[0] in self.data.ids
                and key[1] in self.data.calendar
                and key[1] <= self.data.days[-1],
                "MINUTE_SESSION_ROW_IDENTITY",
            )
            require(
                row["purpose"] == purpose and row["price_basis"] == "raw", "MINUTE_SESSION_PURPOSE"
            )
            available = self._available(row)
            earliest = "09:30" if purpose == "opening_price" else "15:00"
            require(
                available >= stamp(key[1] + "T" + earliest + ":00+08:00"),
                "MINUTE_SESSION_FUTURE_OBSERVATION",
            )
            for field in fields:
                value = number(row[field], zero=field == "volume")
                if field == "volume":
                    require(value == value.to_integral_value(), "MINUTE_VOLUME_NOT_SHARES")
                    require(row["volume_unit"] == "share", "MINUTE_SESSION_VOLUME_UNIT")
            result[key] = {**row, "_available": available}
        return result

    def _history(self, rows, sessions):
        ends = set()
        for day, intervals in sessions.items():
            require(
                day in self.data.calendar and day < self.data.days[0],
                "MINUTE_WARMUP_OUTSIDE_HISTORY",
            )
            prior_end = None
            for start, finish in intervals:
                a, b = stamp(start), stamp(finish)
                require(
                    a.date().isoformat() == b.date().isoformat() == day
                    and a < b
                    and not (a.second or b.second or a.microsecond or b.microsecond)
                    and "09:30" <= a.strftime("%H:%M") < b.strftime("%H:%M") <= "15:00"
                    and not (a.strftime("%H:%M") < "13:00" and b.strftime("%H:%M") > "11:30")
                    and (prior_end is None or prior_end < a),
                    "MINUTE_WARMUP_SESSION",
                )
                ends.update(
                    a + timedelta(minutes=i)
                    for i in range(1, int((b - a).total_seconds() / 60) + 1)
                )
                prior_end = b
        values = {}
        for raw in rows:
            if self.data.research:
                from ashare_quant.backtests.minute.bounded import interval

                a, b = interval(raw)
            else:
                a, b = stamp(raw["bar_start"]), stamp(raw["bar_end"])
            key = raw["security"], b
            require(
                key not in values
                and key[0] in self.data.ids
                and b in ends
                and b - a == timedelta(minutes=1),
                "MINUTE_WARMUP_INTERVAL",
            )
            require(raw.get("evidence"), "MINUTE_WARMUP_EVIDENCE")
            if self.data.visibility == "declared_available_at":
                available = stamp(raw["available_at"])
            else:
                require(raw.get("available_at") is None, "MINUTE_WARMUP_UNKNOWN_AVAILABILITY")
                available = b
            require(available >= b, "MINUTE_UNFINISHED_BAR_VISIBLE")
            o, h, low, c = (number(raw[f]) for f in ("open", "high", "low", "close"))
            require(low <= min(o, c) <= max(o, c) <= h, "MINUTE_INVALID_OHLC")
            volume = number(raw["volume"], zero=True)
            require(volume == volume.to_integral_value(), "MINUTE_VOLUME_NOT_SHARES")
            number(raw["amount"], zero=True)
            values[key] = {**raw, "_start": a, "_end": b, "_available": available}
        require(
            set(values) == {(s, at) for s in self.data.ids for at in ends}, "MINUTE_WARMUP_COVERAGE"
        )
        return values

    def visible_open(self, sid, at):
        row = self.opening[sid, str(at.date())]
        require(row["_available"] <= at, "MINUTE_DAY_OPEN_NOT_YET_VISIBLE")
        return float(row["price"])

    def visible_daily(self, sid, at, start, end):
        require(start <= end <= str(at.date()), "AQUANT_FUTURE_DATA_REQUEST")
        return [
            r
            for (s, d), r in sorted(self.daily.items())
            if s == sid and start <= d <= end and r["_available"] <= at
        ]
