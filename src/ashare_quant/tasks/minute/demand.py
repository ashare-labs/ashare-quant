"""Runtime reads against one frozen publication; subscriptions are not data permissions.

Only actual API calls determine historical demand. No strategy discovery pass runs.
Execution retains the existing observed-state/corporate model, loaded per security/day.
"""

from datetime import date, timedelta
from pathlib import Path

from ashare_data.product.market_release import ReleasedDailyProduct, minute_prices
from ashare_data.product.released_corporate import ReleasedCorporateView
from ashare_data.product.released_references import ReleasedReferences
from ashare_data.storage.market_release import MarketRelease

from ashare_quant.backtests.minute.inputs import number, stamp
from ashare_quant.contracts import DomainError
from ashare_quant.portfolio.corporate import MODEL, schedule_rows
from ashare_quant.providers.daily.history import frozen_factors, history_prices
from ashare_quant.storage.artifacts import atomic, digest
from ashare_quant.tasks.minute.prepare import prepare_market, require
from ashare_quant.tasks.policy import ServicePolicy


def run_scope(pin, start, end, *, release=None):
    release = release or MarketRelease(
        pin["root"], pin["release_id"], expected_manifest_sha256=pin["manifest_sha256"]
    )
    with release.metadata() as db:
        calendar = [r[0] for r in db.execute("SELECT date FROM calendar ORDER BY date")]
    require(
        calendar and calendar[0] <= start <= end <= calendar[-1],
        "PYTHON_CALENDAR_GAP",
        f"Requested interval {start} to {end} extends outside the frozen calendar "
        f"({calendar[0] if calendar else 'empty'} to {calendar[-1] if calendar else 'empty'})",
        {"requested_start": start, "requested_end": end,
         "calendar_start": calendar[0] if calendar else None,
         "calendar_end": calendar[-1] if calendar else None,
         "release_id": release.release_id},
    )
    days = [d for d in calendar if start <= d <= end]
    require(
        days and calendar.index(days[0]) > 0 and calendar.index(days[-1]) + 1 < len(calendar),
        "PYTHON_CALENDAR_GAP",
        "Execution needs a previous and a following session in the frozen calendar; "
        f"usable session boundaries are {calendar[1] if len(calendar) > 2 else None} "
        f"to {calendar[-2] if len(calendar) > 2 else None}",
    )
    return days, calendar


class ReferenceSnapshot(ReleasedReferences):
    """Task-local results of actual frozen reference queries, charged to its budget."""

    def __init__(self, data):
        super().__init__(data.pin, data.pin, release=data.owner.release)
        self.data, self.cache = data, {}

    def query(self, table, condition, parameters):
        key = table, condition, tuple(parameters)
        if key not in self.cache:
            rows = super().query(table, condition, parameters)
            self.data.check_budget(len(rows))
            self.data.reference_rows += len(rows)
            self.data._retain(
                {"api": "execution_references", "table": table,
                 "query_sha256": digest(key), "rows_sha256": digest(rows), "rows": len(rows)},
                len(rows),
            )
            self.cache[key] = rows
        return self.cache[key]


class DemandInputs:
    demand = True
    research = True
    generic_research = False
    full_session_input = True
    no_price_limits = False
    session_data = None
    state_visibility = "posthoc_daily_replay"
    state_replay_used = True

    def __init__(self, pin, request, root, policy=None, factors=None):
        self.pin, self.request, self.root = pin, request, Path(root)
        self.policy = policy or ServicePolicy()
        self.owner = ReleasedDailyProduct({"market_release": pin})
        self.days, self.calendar = run_scope(
            pin, str(request.start_date), str(request.end_date), release=self.owner.release
        )
        self.frequency = request.frequency
        self.generic_research = self.frequency == "1m"
        self.previous = {d: self.calendar[self.calendar.index(d) - 1] for d in self.days}
        self.ids = []  # Native instruments encountered; distinct from API.universe and read cache.
        self.register(request.securities)
        self.rows, self.minutes, self.components, self.corporate = {}, {}, {}, {}
        self.reads, self.queries, self.read_count = [], [], 0
        self.references, self.reference_rows = None, 0
        self.bars, self.gaps = {}, {}
        self.corporate_actions, self.actions = [], []
        self.factors = frozen_factors(self.root, factors)
        self.ends, self.clocks, self.sessions = {}, {}, {}
        for day in self.days:
            periods = [
                (stamp(day + "T09:30:00+08:00"), stamp(day + "T11:30:00+08:00")),
                (stamp(day + "T13:00:00+08:00"), stamp(day + "T15:00:00+08:00")),
            ]
            self.sessions[day] = periods
            self.clocks[day] = (
                [stamp(day + "T09:30:00+08:00")]
                if self.frequency == "1d"
                else [
                    a + timedelta(minutes=n)
                    for a, b in periods
                    for n in range(1, int((b - a).total_seconds() / 60) + 1)
                ]
            )
            self.ends[day] = self.clocks[day]
        self.document = {
            "schema": "aq-python-demand-input.v2",
            "real_market_data": True,
            "release": pin,
            "frequency": self.frequency,
            "days": self.days,
            "calendar": self.calendar,
            "corporate_account_model": MODEL,
            "history_factors": factors,
            "limitations": [
                "Release-pinned retrospective observations; historical PIT is not certified",
                "Directory uses recorded listing/code intervals; historical names and complete historical membership are not certified",
                "Execution retains the declared main-board daily-preclose limit and source-scoped corporate model",
            ],
        }

    def check_budget(self, rows):
        require(
            type(rows) is int and 0 <= rows <= self.policy.query_rows,
            "PYTHON_DATA_BUDGET",
            "Query exceeds configured query_rows",
            {"resource": "query_rows", "requested": rows, "limit": self.policy.query_rows},
        )

    def _retain(self, response, count):
        self.read_count += count
        require(
            self.read_count <= self.policy.read_rows,
            "PYTHON_DATA_BUDGET",
            "Frozen reads exceed configured read_rows",
            {"resource": "read_rows", "observed": self.read_count, "limit": self.policy.read_rows},
        )
        require(
            len(self.rows)
            + sum(len(v) for v in self.minutes.values())
            + len(self.bars)
            + len(self.gaps)
            + self.reference_rows
            <= self.policy.cached_rows,
            "PYTHON_DATA_BUDGET",
            "Cache exceeds configured cached_rows",
            {"resource": "cached_rows", "limit": self.policy.cached_rows},
        )
        self.reads.append(response)

    def securities(self, ids):
        require(
            len(ids) == len(set(ids)) and all(s in self.owner.release.identifiers for s in ids),
            "JQ_UNKNOWN_RELEASE_SECURITY",
            "Security is not in the frozen release",
            {"securities": ids},
        )
        return list(ids)

    def register(self, ids):
        self.securities(list(ids))
        for sid in ids:
            if sid not in self.ids:
                self.ids.append(sid)

    def catalogue(self, as_of):
        items, offset = [], 0
        while True:
            response = self.owner.catalogue(limit=500, offset=offset, as_of=as_of)
            items.extend(response["securities"])
            self.check_budget(len(items))
            if len(items) >= response["total"]:
                break
            offset += len(response["securities"])
        receipt = {
            k: response[k]
            for k in ("receipt", "as_of", "limitations", "unknown_listing_securities")
        }
        self.queries.append(
            {
                "api": "catalogue",
                "as_of": as_of,
                "rows": len(items),
                "rows_sha256": digest(items),
                **receipt,
            }
        )
        return items, receipt

    def _daily(self, sid, days):
        missing = [d for d in days if (sid, d) not in self.rows]
        if missing:
            # Only the requested bounded range, never all published securities.
            response = self.owner.prices([sid], missing[0], missing[-1])
            observed = {r["date"]: r for r in response["rows"]}
            for d in missing:
                self.rows[sid, d] = observed.get(d)
            self._retain(
                {
                    "api": "prices",
                    "security": sid,
                    "start": missing[0],
                    "end": missing[-1],
                    "receipt": response["receipt"],
                    "gaps": response["gaps"],
                },
                len(response["rows"]),
            )
        return [self.rows[sid, d] for d in days]

    def _row(self, sid, day):
        row = self._daily(sid, [day])[0]
        require(
            row is not None,
            "PYTHON_DAILY_GAP",
            f"Requested daily observation is missing: {sid} on {day}",
            {"security": sid, "date": day},
        )
        return row

    def daily_history(
        self,
        sid,
        at,
        *,
        count=None,
        start=None,
        end=None,
        include_now=True,
        skip_paused=False,
        fields=(),
    ):
        cutoff = end or at
        require(cutoff <= at, "JQ_FUTURE_DATA_REQUEST", "Future observations are not visible")
        eligible = [
            d
            for d in self.calendar
            if stamp(d + "T15:00:00+08:00") <= cutoff
            and (include_now or stamp(d + "T15:00:00+08:00") != at)
        ]
        require(
            start is None or start >= self.calendar[0],
            "PYTHON_CALENDAR_GAP",
            "Requested history begins before the frozen calendar",
            {"start": start},
        )
        days = [d for d in eligible if start is None or d >= start]
        self.check_budget(count if count is not None else len(days))
        selected = []
        if count is not None:
            # Expand backwards only when pause filtering actually needs older rows.
            while days and len(selected) < count:
                take = min(len(days), count - len(selected))
                batch, days = days[-take:], days[:-take]
                self._daily(sid, batch)
                for d in reversed(batch):
                    row = self._row(sid, d)
                    if skip_paused and self.is_paused(sid, d):
                        continue
                    selected.append(row)
            selected.reverse()
        else:
            self._daily(sid, days)
            selected = [
                self._row(sid, d) for d in days if not skip_paused or not self.is_paused(sid, d)
            ]
        require(
            selected and (count is None or len(selected) == count),
            "JQ_INSUFFICIENT_VISIBLE_HISTORY",
            "Frozen publication cannot supply the requested history",
            {"security": sid, "requested": count, "returned": len(selected), "start": start},
        )
        for row in selected:
            for field in fields:
                actual = "amount" if field == "money" else field
                try:
                    number(row.get(actual), zero=actual in {"volume", "amount"})
                except (ValueError, ArithmeticError) as exc:
                    raise DomainError(
                        "PYTHON_HISTORY_FIELD_GAP",
                        "Requested field is unavailable",
                        422,
                        details={"security": sid, "date": row["date"], "field": field},
                    ) from exc
        self.queries.append(
            {
                "api": "daily_history",
                "security": sid,
                "count": count,
                "start": start,
                "end": cutoff.isoformat(),
                "knowledge_at": at.isoformat(),
                "fields": list(fields),
                "dates": [r["date"] for r in selected],
                "rows_sha256": digest(selected),
            }
        )
        return selected

    def history_prices(self, rows, adjustment, basis_date):
        receipt = {"release": self.pin, "rows_sha256": digest(rows)}
        receipt["receipt_sha256"] = digest(receipt)
        return history_prices(rows, receipt, self.factors, adjustment, basis_date)

    def is_paused(self, sid, day):
        value = self._row(sid, day).get("tradestatus")
        require(
            value in {"0", "1"},
            "PYTHON_DAILY_HISTORY_STATE_GAP",
            "Pause filtering needs observed trading status; raw queries can use skip_paused=False",
            {"security": sid, "date": day, "field": "tradestatus"},
        )
        return value == "0"

    def _minute_day(self, sid, day):
        if (sid, day) not in self.minutes:
            response = minute_prices(
                {"market_release": self.pin}, sid, day + "T09:30:00+08:00", day + "T15:00:00+08:00",
                release=self.owner.release,
            )
            rows = {}
            for original in response["rows"]:
                at = stamp(original["source_label"])
                clock = at.strftime("%H:%M")
                if not ("09:30" < clock <= "11:30" or "13:00" < clock <= "15:00"):
                    continue
                rows[at] = {
                    **original,
                    "_end": at,
                    "_available": at,
                    "bar_end": at.isoformat(),
                    "source_label": at.isoformat(),
                }
            self.minutes[sid, day] = rows
            self._retain(
                {
                    "api": "minute_prices",
                    "security": sid,
                    "date": day,
                    "binding": response["binding"],
                    "response_sha256": digest(response),
                },
                len(rows),
            )
        return self.minutes[sid, day]

    def minute_history(
        self, sid, at, *, count=None, start=None, end=None, include_now=True, fields=()
    ):
        cutoff = end or at
        require(cutoff <= at, "JQ_FUTURE_DATA_REQUEST", "Future observations are not visible")
        require(
            start is None or str(start.date()) >= self.calendar[0],
            "PYTHON_CALENDAR_GAP",
            "Requested minutes begin before the frozen calendar",
        )
        slots = []
        for day in reversed([d for d in self.calendar if d <= str(cutoff.date())]):
            if start is not None and day < str(start.date()):
                break
            minutes = list(range(571, 691)) + list(range(781, 901))
            for minute in reversed(minutes):
                t = stamp(f"{day}T{minute // 60:02d}:{minute % 60:02d}:00+08:00")
                if t <= cutoff and (include_now or t != at) and (start is None or t >= start):
                    slots.append(t)
                    self.check_budget(len(slots))
                    if count is not None and len(slots) == count:
                        break
            if count is not None and len(slots) == count:
                break
        slots.reverse()
        require(
            slots and (count is None or len(slots) == count),
            "JQ_INSUFFICIENT_VISIBLE_HISTORY",
            "Frozen publication cannot supply the requested minutes",
        )
        selected = []
        for t in slots:
            row = self._minute_day(sid, str(t.date())).get(t)
            require(
                row is not None,
                "PYTHON_MINUTE_GAP",
                "Requested minute is missing",
                {"security": sid, "source_label": t.isoformat()},
            )
            row = dict(row)
            full_gap = row.get("missing_reason") == "confirmed_invalid_price"
            for field in fields:
                actual = "amount" if field == "money" else field
                if full_gap:
                    continue  # Preserve the owner's explicit unavailable slot as NaN.
                try:
                    number(row.get(actual), zero=actual in {"volume", "amount"})
                except (ValueError, ArithmeticError) as exc:
                    raise DomainError(
                        "PYTHON_HISTORY_FIELD_GAP",
                        "Requested minute field is unavailable",
                        422,
                        details={"security": sid, "source_label": t.isoformat(), "field": field},
                    ) from exc
            if not full_gap:
                row.pop("missing_reason", None)
            selected.append(row)
        self.queries.append(
            {
                "api": "minute_history",
                "security": sid,
                "count": count,
                "start": start.isoformat() if start else None,
                "fields": list(fields),
                "knowledge_at": at.isoformat(),
                "end": cutoff.isoformat(),
                "labels": [r["bar_end"] for r in selected],
                "rows_sha256": digest(
                    [{k: v for k, v in r.items() if not k.startswith("_")} for r in selected]
                ),
            }
        )
        return selected

    def visible(self, sid, at, *, count=None, end=None, include_now=True, preserve_missing=False):
        fields = ["open", "high", "low", "close", "volume", "money"]
        if self.frequency == "1d":
            return [
                {
                    **r,
                    "_end": stamp(r["date"] + "T15:00:00+08:00"),
                    "bar_end": r["date"] + "T15:00:00+08:00",
                }
                for r in self.daily_history(
                    sid, at, count=count, end=end, include_now=include_now, fields=fields
                )
            ]
        return self.minute_history(
            sid, at, count=count, end=end, include_now=include_now, fields=fields
        )

    def _corporate(self, sid, day):
        key = sid + "|" + day
        start = day if self.frequency == "1d" else self.previous[day]
        if key not in self.corporate:
            if self.references is None:
                self.references = ReferenceSnapshot(self)
            view = ReleasedCorporateView(self.references, [sid], start, day)
            corp = view.export_aq(
                [sid],
                start=start,
                end=day,
                tax_model="gross_cash_no_tax",
                accept_source_scoped_coverage=True,
            )
            doc = {"corporate_schedule": corp, "corporate_account_model": MODEL}
            actions = schedule_rows(doc, [sid], {"start": start, "end": day}, self.calendar)
            self.corporate[key] = corp
            known = {a["entitlement_key"]: digest(a) for a in self.corporate_actions}
            for action in actions:
                if action["entitlement_key"] not in known:
                    self.corporate_actions.append(action)
                else:
                    require(
                        known[action["entitlement_key"]] == digest(action),
                        "CORPORATE_REVISION_RECONCILIATION_REQUIRED",
                        "Frozen action changed between reads",
                    )
        return self.corporate[key]

    def component(self, sid, day):
        key = sid, day
        if key not in self.components:
            self.register([sid])
            req = self.request.model_copy(
                update={
                    "securities": (sid,),
                    "start_date": date.fromisoformat(day),
                    "end_date": date.fromisoformat(day),
                    "warmup_sessions": 1,
                }
            )
            corp = self._corporate(sid, day)
            if self.frequency == "1d":
                from ashare_quant.backtests.daily.python_inputs import DailyPythonInputs

                prev = self.calendar[self.calendar.index(day) - 1]
                rows = self._daily(sid, [prev, day])
                require(
                    all(r is not None for r in rows),
                    "PYTHON_DAILY_GAP",
                    f"Execution needs daily bars for {sid} on {prev} and {day}; "
                    "at least one observation is missing",
                    {"security": sid, "date": day},
                )
                daily = {
                    "rows": rows,
                    "receipt": {"release": self.pin, "rows_sha256": digest(rows)},
                }
                metadata, daily, _ = prepare_market(
                    self.pin, req, owner=self.owner, daily=daily, corporate=corp
                )
                doc = {
                    **metadata,
                    "schema": "aq-python-daily-input.v1",
                    "producer": {"name": "ashare-data"},
                    "daily": rows,
                    "daily_receipt": daily["receipt"],
                    "warmup_sessions": 1,
                }
                part = DailyPythonInputs(
                    None, document=doc, factors=self.factors, execution_only=True
                )
            else:
                from ashare_quant.backtests.minute.inputs import MinuteInputs
                from ashare_quant.tasks.minute.prepare import prepare

                directory = self.root / "execution-cache" / f"{sid}-{day}"
                directory.mkdir(parents=True, exist_ok=True)
                prepare(self.pin, req, directory, owner=self.owner, corporate=corp)
                part = MinuteInputs(
                    directory / "input.json",
                    accept_assumed=True,
                    accept_state_replay=True,
                    accept_research=True,
                )
                self.bars.update(part.bars)
                self.gaps.update(part.gaps)
                self._retain(
                    {
                        "api": "execution_minutes",
                        "security": sid,
                        "date": day,
                        "input_sha256": part.sha256,
                    },
                    len(part.bars),
                )
            self.components[key] = part
        return self.components[key]

    def row(self, sid, day):
        if day not in self.days:
            return self._row(sid, day)
        return self.component(sid, day).row(sid, day)

    def state(self, sid, day, *, at=None):
        return self.component(sid, day).state(sid, day, at=at)

    def current(self, sid, at):
        day = str(at.date())
        if self.frequency == "1d":
            field = "close" if at.hour >= 15 or at.hour < 9 else "open"
            if at.hour * 60 + at.minute < 570:
                day, field = self.previous[day], "close"
            return self.observed_value(sid, day, field)
        return self.component(sid, day).current(sid, at)

    valuation_price = current

    def quote_price(self, sid, at):
        """A read-only last price does not prepare a native execution component."""
        if self.frequency == "1d":
            return self.current(sid, at)
        if at.strftime("%H:%M") < "09:31":
            return self.observed_value(sid, self.previous[str(at.date())], "close")
        row = self.minute_history(sid, at, count=1, fields=["close"])[-1]
        return float(row["close"])

    def valuation_mark(self, sid, at):
        return self.component(sid, str(at.date())).valuation_mark(sid, at)

    def observed_value(self, sid, day, field):
        value = self._row(sid, day).get(field)
        try:
            number(value, zero=field in {"volume", "amount"})
        except (ValueError, ArithmeticError) as exc:
            raise DomainError(
                "PYTHON_HISTORY_FIELD_GAP",
                "Requested field is unavailable",
                422,
                details={"security": sid, "date": day, "field": field},
            ) from exc
        return float(value)

    def check_order_price(self, sid, price, *, at):
        return self.component(sid, str(at.date())).check_order_price(sid, price, at=at)

    def price_bounds(self, sid, day, *, at):
        return self.component(sid, day).price_bounds(sid, day, at=at)

    def execution_denial(self, sid, at):
        return self.component(sid, str(at.date())).execution_denial(sid, at)

    def validate_execution(self, config):
        expected = (
            ("daily_open", "daily", "daily")
            if self.frequency == "1d"
            else ("next_open", "jq_minute", "strict_minute")
        )
        require(
            tuple(config[k] for k in ("matching", "lifecycle", "capacity_policy")) == expected,
            "PYTHON_EXECUTION_MODEL",
            "Use the existing declared execution model",
        )

    def preflight(self):
        return {
            "ready": True,
            "data_access": "demand.v2",
            "frequency": self.frequency,
            "securities": list(self.ids),
            "trading_dates": self.days,
            "daily_rows": sum(r is not None for r in self.rows.values()),
            "history_factors": self.document["history_factors"],
            "corporate_schedule_sha256": digest(self.corporate),
            "historical_pit_verified": False,
            "state_visibility": self.state_visibility,
            "resource_budget": self.policy.model_dump(mode="json"),
            "limitations": self.document["limitations"],
        }

    def finalize(self):
        self.document.update(
            securities=list(self.ids),
            daily=[r for r in self.rows.values() if r is not None],
            reads=self.reads,
            queries=self.queries,
            corporate_schedules=self.corporate,
        )
        atomic(self.root / "input.json", self.document)
        report = {
            **self.preflight(),
            "release": self.owner.release.receipt(),
            "input_sha256": digest(self.document),
            "read_rows": self.read_count,
            "query_count": len(self.queries),
        }
        atomic(self.root / "preparation.json", report)
        return report

    def close(self):
        if self.references is not None:
            self.references.close()
            self.references = None
