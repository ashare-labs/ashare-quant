"""Runtime reads against one frozen publication; subscriptions are not data permissions.

Only actual API calls determine historical demand. No strategy discovery pass runs.
Execution retains the existing observed-state/corporate model, loaded per security/day.
"""

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from ashare_data.product.market_release import ReleasedDailyProduct, minute_prices
from ashare_data.product.released_corporate import ReleasedCorporateView
from ashare_data.product.released_references import ReleasedReferences
from ashare_data.storage.market_release import MarketRelease

from ashare_quant.contracts import DomainError
from ashare_quant.core.validation import number, stamp
from ashare_quant.portfolio.corporate import MODEL, schedule_rows
from ashare_quant.providers.daily.history import frozen_factors, history_prices
from ashare_quant.storage.artifacts import atomic, digest
from ashare_quant.storage.streams import Journal, compress, file_hash
from ashare_quant.tasks.minute.prepare import (
    _positive_trading_bar,
    carry_daily,
    cmes_facts,
    listing_facts,
    prepare_market,
    query_cmes_fact,
    require,
    require_dated_states,
    require_listing_window,
)
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


class DayValues(Mapping):
    """Calendar-sized keys, one lazily generated day's clocks at a time."""
    def __init__(self, days, factory):
        self.days, self.factory, self.day, self.value = days, factory, None, None

    def __getitem__(self, day):
        if day not in self.days:
            raise KeyError(day)
        if self.day != day:
            self.day, self.value = day, self.factory(day)
        return self.value

    def __iter__(self):
        return iter(self.days)

    def __len__(self):
        return len(self.days)


def periods(day):
    return [(stamp(day + "T09:30:00+08:00"), stamp(day + "T11:30:00+08:00")),
            (stamp(day + "T13:00:00+08:00"), stamp(day + "T15:00:00+08:00"))]


class ReferenceSnapshot(ReleasedReferences):
    """Reuse corporate reference queries for the current security, never all prior securities."""

    def __init__(self, data):
        super().__init__(data.pin, data.pin, release=data.owner.release)
        self.data, self.cache, self.security = data, {}, None

    def scope(self, security):
        if self.security != security:
            self.cache.clear()
            self.security = security

    def query(self, table, condition, parameters):
        key = table, condition, tuple(parameters)
        if key not in self.cache:
            rows = super().query(table, condition, parameters)
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
    MAX_HISTORY_SECURITIES = 1024
    MAX_SECURITY_WINDOW = 520

    def __init__(self, pin, request, root, policy=None, factors=None, benchmark=None):
        self.pin, self.request, self.root = pin, request, Path(root)
        self.benchmark_pin = benchmark
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
        self.rows, self.history_rows, self.minutes, self.components, self.corporate = {}, {}, {}, {}, {}
        self._history_windows = OrderedDict()
        self._history_keys = {}
        self._journaled_daily = set()
        self._listing_cache = {}
        self.execution_closes, self.daily_assumptions = {}, {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.reads = Journal(self.root / "reads.jsonl.gz")
        self.queries = Journal(self.root / "queries.jsonl.gz")
        self.corporate_evidence = Journal(self.root / "corporate.jsonl.gz")
        self.read_count, self.daily_read_rows, self.component_day = 0, 0, None
        self.references, self.reference_rows, self.benchmark_rows = None, 0, 0
        self._cmes_facts = None
        self._source_fact_cache = OrderedDict()
        self.bars, self.gaps = {}, {}
        self.corporate_actions, self.actions = [], []
        self.factors = frozen_factors(self.root, factors)
        self.sessions = DayValues(self.days, periods)
        self.clocks = DayValues(self.days, lambda day:
            [stamp(day + "T09:30:00+08:00")] if self.frequency == "1d" else
            [a + timedelta(minutes=n) for a, b in periods(day)
             for n in range(1, int((b-a).total_seconds()/60)+1)])
        self.ends = self.clocks
        self.document = {
            "schema": "aq-python-demand-input.v3",
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
                "Missing daily sessions use a prior observed close for valuation and cannot trade; this does not prove suspension",
            ],
        }
        if benchmark is not None:
            self.document["benchmark_release"] = benchmark
        if self.owner.release.manifest.get("cmes"):
            self.document["limitations"].append(
                "CMES source-reported listing dates and source-scoped ST facts do not establish "
                "suspension, historical active eligibility or PIT availability"
            )

    def source_fact(self, method, *arguments):
        """Journal partial facts separately from unchanged raw price reads."""
        from ashare_data.core.model import DataError

        require(self.owner.release.manifest.get("cmes")
                and method in {"listing_date", "st_status", "trading_status"}
                and len(arguments) == (1 if method == "listing_date" else 2),
                "PYTHON_SOURCE_FACT_SCOPE", "Only pin-bound CMES partial facts are supported")
        if self._cmes_facts is None:
            self._cmes_facts = cmes_facts(self.owner.release)
        key = method, tuple(arguments)
        hit = key in self._source_fact_cache
        if hit:
            fact = self._source_fact_cache[key]
            self._source_fact_cache.move_to_end(key)
        else:
            fact = query_cmes_fact(self._cmes_facts, method, *arguments)
        # A retained fact never bypasses the reader's normal metadata identity
        # check. This uses the true physical replica/source and its receipt;
        # it neither re-scans tables nor changes the immutable release binding.
        try:
            self.owner.release.verify(self.owner.release.manifest["metadata"])
        except DataError as exc:
            raise DomainError(exc.code, exc.message, 422, details=exc.details,
                              origin="ashare-data") from exc
        expected_field = {
            "listing_date": "source_reported_listing_date",
            "st_status": "is_st",
            "trading_status": "tradestatus",
        }[method]
        expected_rule = {
            "st_status": "cmes-complete-history-membership.v1",
            "trading_status": "cmes-observed-trading-activity.v1",
        }.get(method)
        require(isinstance(fact, dict) and fact.get("release") == self.owner.release.receipt()
                and fact.get("security") == arguments[0] and fact.get("field") == expected_field
                and (expected_rule is None or (fact.get("date") == arguments[1]
                    and (fact.get("status") != "derived"
                         or fact.get("rule") == expected_rule))),
                "PYTHON_SOURCE_FACT_BINDING", "Partial fact differs from the fixed query identity")
        if not hit:
            self._source_fact_cache[key] = deepcopy(fact)
            # Retention only; eviction never limits queries or source coverage.
            while len(self._source_fact_cache) > 128:
                self._source_fact_cache.popitem(last=False)
        self.queries.append({"api": "cmes_partial_fact", "method": method,
                             "arguments": list(arguments), "fact": deepcopy(fact),
                             "fact_sha256": digest(fact), "cache_hit": hit})
        return deepcopy(fact)

    def is_st(self, sid, day):
        self.securities([sid])
        if self.owner.release.manifest.get("cmes"):
            fact = self.source_fact("st_status", sid, day)
            if type(fact.get("value")) is bool:
                return fact["value"]
            return False
        row = self._daily(sid, [day])[0]
        return row is not None and row.get("is_st") == "1"

    def _retain(self, response, count):
        self.read_count += count
        self.reads.append(response)

    def advance_day(self, day):
        if day != self.component_day:
            # Only execution data expires. Positions, orders and entitlements live
            # in the native account and must survive subscription/cache changes.
            self.rows.clear()
            self.components.clear()
            self.bars.clear()
            self.gaps.clear()
            self.corporate.clear()
            self.component_day = day

    def securities(self, ids):
        require(
            len(ids) == len(set(ids)) and all(s in self.owner.release.identifiers for s in ids),
            "AQUANT_UNKNOWN_RELEASE_SECURITY",
            "Security is not in the frozen release",
            {"securities": ids},
        )
        return list(ids)

    def register(self, ids):
        self.securities(list(ids))
        for sid in ids:
            require(self.owner.release.instruments.get(sid, {}).get("asset_class", "equity") == "equity",
                    "AQUANT_NOT_TRADABLE_SECURITY", "The current execution model trades equities only",
                    {"security": sid})
            if sid not in self.ids:
                self.ids.append(sid)

    def catalogue(self, as_of):
        items, offset = [], 0
        while True:
            response = self.owner.catalogue(limit=500, offset=offset, as_of=as_of)
            items.extend(r for r in response["securities"] if r.get("asset_class", "equity") == "equity")
            if offset + len(response["securities"]) >= response["total"]:
                break
            offset += len(response["securities"])
        receipt = {
            k: response[k]
            for k in ("receipt", "as_of", "limitations", "unknown_listing_securities")
        }
        receipt["listing_coverage"] = ("partial" if receipt["unknown_listing_securities"]
                                       else "recorded_intervals")
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

    def listing_info(self, sids):
        missing = [s for s in dict.fromkeys(sids) if s not in self._listing_cache]
        if missing:
            if self._cmes_facts is None and self.owner.release.manifest.get("cmes"):
                self._cmes_facts = cmes_facts(self.owner.release)
            facts = listing_facts(
                self.owner.release,
                missing,
                facts=self._cmes_facts,
                fact_query=(
                    (lambda method, *args: self.source_fact(method, *args))
                    if self.owner.release.manifest.get("cmes")
                    else None
                ),
            )
            self._listing_cache.update(facts)
        return {s: self._listing_cache[s] for s in sids}

    @staticmethod
    def classify_history_day(sid, d, row, *, ipo=None):
        """Classify a calendar session into pre_listing, missing, paused, active, or unknown_status."""
        if ipo is not None and d < ipo:
            return "pre_listing"
        if row is None:
            return "missing"
        status = row.get("tradestatus")
        if status == "0":
            return "paused"
        if status == "1" or _positive_trading_bar(row):
            return "active"
        return "unknown_status"

    def _update_history_window(self, sid, requested_days, *, evict=True):
        if not requested_days:
            return
        keys = self._history_keys.setdefault(sid, set())
        prev_range = self._history_windows.get(sid)
        if prev_range is None or requested_days[-1] > prev_range[1]:
            lo, hi = requested_days[0], requested_days[-1]
            for d in [d for d in keys if d < lo or d > hi]:
                keys.discard(d)
                self.history_rows.pop((sid, d), None)
            self._history_windows[sid] = (lo, hi)
        else:
            lo = min(prev_range[0], requested_days[0])
            hi = prev_range[1]
            limit = max(len(requested_days), self.MAX_SECURITY_WINDOW)
            if len(keys) > limit:
                ordered = sorted(keys)
                for d in ordered[:-limit]:
                    keys.discard(d)
                    self.history_rows.pop((sid, d), None)
                lo = min(keys) if keys else requested_days[0]
            self._history_windows[sid] = (lo, hi)
        self._history_windows.move_to_end(sid)
        if evict:
            while len(self._history_windows) > self.MAX_HISTORY_SECURITIES:
                evicted_sid, _ = self._history_windows.popitem(last=False)
                for d in self._history_keys.pop(evicted_sid, ()):
                    self.history_rows.pop((evicted_sid, d), None)

    def daily_batch(self, sids, days, *, history=True):
        unique_sids = list(dict.fromkeys(sids))
        if not unique_sids or not days:
            return {sid: [] for sid in unique_sids}
        ordered_days = sorted(set(days))
        cache = self.history_rows if history else self.rows
        resolved = {}
        missing_by_sid = {}
        for sid in unique_sids:
            missing = []
            for d in ordered_days:
                if (sid, d) in cache:
                    resolved[sid, d] = cache[sid, d]
                else:
                    missing.append(d)
            if missing:
                missing_by_sid[sid] = (missing[0], missing[-1])

        groups = {}
        for sid, bounds in missing_by_sid.items():
            groups.setdefault(bounds, []).append(sid)
        for (start, end), group_sids in groups.items():
            span = [d for d in self.calendar if start <= d <= end]
            for index in range(0, len(group_sids), 500):
                chunk = group_sids[index : index + 500]
                response = self.owner.prices(chunk, start, end)
                by_pair = {(r["security"], r["date"]): r for r in response["rows"]}
                for sid in chunk:
                    for d in span:
                        if (sid, d) not in resolved:
                            resolved[sid, d] = by_pair.get((sid, d))
                fresh_rows = [
                    r
                    for r in response["rows"]
                    if (r["security"], r["date"]) not in self._journaled_daily
                ]
                for r in fresh_rows:
                    self._journaled_daily.add((r["security"], r["date"]))
                if fresh_rows or not response["rows"]:
                    raw_receipt = response.get("receipt") or {}
                    self._retain(
                        {
                            "api": "prices",
                            **(
                                {"security": chunk[0]}
                                if len(chunk) == 1
                                else {"securities": chunk}
                            ),
                            "start": start,
                            "end": end,
                            "receipt": {
                                "release": raw_receipt.get("release"),
                                "receipt_sha256": raw_receipt.get("receipt_sha256"),
                            },
                            "gaps_count": len(response.get("gaps", ())),
                            "data": fresh_rows,
                        },
                        len(response["rows"]),
                    )
                self.daily_read_rows += len(response["rows"])

        output = {sid: [resolved[sid, d] for d in days] for sid in unique_sids}
        if history:
            for sid in unique_sids:
                keys = self._history_keys.setdefault(sid, set())
                for d in ordered_days:
                    self.history_rows[sid, d] = resolved[sid, d]
                    keys.add(d)
                self._update_history_window(sid, ordered_days, evict=False)
            while len(self._history_windows) > self.MAX_HISTORY_SECURITIES:
                evicted_sid, _ = self._history_windows.popitem(last=False)
                for d in self._history_keys.pop(evicted_sid, ()):
                    self.history_rows.pop((evicted_sid, d), None)
        else:
            for sid in unique_sids:
                for d in ordered_days:
                    self.rows[sid, d] = resolved[sid, d]
        return output

    def prefetch_daily(self, sids, days, *, history=True):
        return self.daily_batch(sids, days, history=history)

    def _daily(self, sid, days, *, history=False):
        if not days:
            return []
        return self.daily_batch([sid], days, history=history)[sid]

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
        require(cutoff <= at, "AQUANT_FUTURE_DATA_REQUEST", "Future observations are not visible")
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
        ipo = self.listing_info([sid])[sid]["ipo"]
        hit_pre_listing = False
        selected = []
        if count is not None:
            # Expand backwards only when confirmed-pause filtering needs older rows.
            while days and len(selected) < count:
                take = min(len(days), count - len(selected))
                batch, days = days[-take:], days[:-take]
                batch_rows = self._daily(sid, batch, history=True)
                for d, row in reversed(list(zip(batch, batch_rows))):
                    kind = self.classify_history_day(sid, d, row, ipo=ipo)
                    if kind == "pre_listing":
                        hit_pre_listing = True
                        days = []
                        break
                    require(
                        kind != "missing",
                        "PYTHON_DAILY_GAP",
                        f"Requested daily observation is missing: {sid} on {d}",
                        {"security": sid, "date": d},
                    )
                    if skip_paused:
                        require(
                            kind != "unknown_status",
                            "PYTHON_DAILY_HISTORY_STATE_GAP",
                            f"Trading status is unknown without explicit suspension evidence: {sid} on {d}",
                            {"security": sid, "date": d, "field": "tradestatus"},
                        )
                        if kind == "paused":
                            continue
                    selected.append(row)
                    if len(selected) == count:
                        break
            selected.reverse()
        else:
            selected = []
            for d, row in zip(days, self._daily(sid, days, history=True)):
                kind = self.classify_history_day(sid, d, row, ipo=ipo)
                if kind == "pre_listing":
                    hit_pre_listing = True
                    continue
                require(
                    kind != "missing",
                    "PYTHON_DAILY_GAP",
                    f"Requested daily observation is missing: {sid} on {d}",
                    {"security": sid, "date": d},
                )
                if skip_paused:
                    require(
                        kind != "unknown_status",
                        "PYTHON_DAILY_HISTORY_STATE_GAP",
                        f"Trading status is unknown without explicit suspension evidence: {sid} on {d}",
                        {"security": sid, "date": d, "field": "tradestatus"},
                    )
                    if kind == "paused":
                        continue
                selected.append(row)
        if not selected or (count is not None and len(selected) < count):
            msg = (
                f"上市历史不足：{sid} 自上市日 {ipo} 起可用 K 线仅 {len(selected)} 根（请求 {count or 1} 根）"
                if hit_pre_listing and ipo is not None
                else "Frozen publication cannot supply the requested history"
            )
            raise DomainError(
                "AQUANT_INSUFFICIENT_VISIBLE_HISTORY",
                msg,
                422,
                details={
                    "security": sid,
                    "requested": count,
                    "returned": len(selected),
                    "start": start,
                    **(
                        {"ipo": ipo, "reason": "insufficient_listed_history"}
                        if hit_pre_listing and ipo is not None
                        else {}
                    ),
                },
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
            }
        )
        return selected

    def history_prices(self, rows, adjustment, basis_date):
        if adjustment is None:
            return history_prices(rows, {"release": self.pin}, self.factors, None, basis_date)
        receipt = {"release": self.pin, "rows_sha256": digest(rows)}
        receipt["receipt_sha256"] = digest(receipt)
        return history_prices(rows, receipt, self.factors, adjustment, basis_date)

    def is_paused(self, sid, day, *, row=None):
        current = row if row is not None else self._daily(sid, [day])[0]
        if current is None:
            return True
        value = current.get("tradestatus")
        if value in {"0", "1"}:
            return value == "0"
        return not _positive_trading_bar(current)

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
            self.minutes = {(sid, day): rows}
            self._retain(
                {
                    "api": "minute_prices",
                    "security": sid,
                    "date": day,
                    "binding": response["binding"],
                    "response_sha256": digest(response),
                    "data": response["rows"],
                },
                len(rows),
            )
        return self.minutes[sid, day]

    def minute_history(
        self, sid, at, *, count=None, start=None, end=None, include_now=True, fields=()
    ):
        cutoff = end or at
        require(cutoff <= at, "AQUANT_FUTURE_DATA_REQUEST", "Future observations are not visible")
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
                    if count is not None and len(slots) == count:
                        break
            if count is not None and len(slots) == count:
                break
        slots.reverse()
        require(
            slots and (count is None or len(slots) == count),
            "AQUANT_INSUFFICIENT_VISIBLE_HISTORY",
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
            self.references.scope(sid)
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
            self.corporate_evidence.append({"key": key, "schedule": corp})
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

    def _execution_row(self, sid, day, row=None):
        """Keep price queries raw; carry missing observations only for account execution/valuation."""
        if row is not None:
            return row
        cached = self.execution_closes.get(sid)
        if cached is not None and cached["date"] == day:
            return cached
        end = self.calendar.index(day)
        while end:
            start = max(0, end - 64)
            previous = self._daily(sid, self.calendar[start:end], history=True)
            observed = next((r for r in reversed(previous) if r is not None), None)
            if observed is not None:
                return carry_daily(observed, day)
            end = start
        require(False, "PYTHON_DAILY_GAP",
                f"No earlier observed price for {sid} on {day}", {"security": sid, "date": day})

    def _record_daily_assumption(self, row):
        key = row["security"], row["date"]
        if row.get("assumption") and key not in self.daily_assumptions:
            value = {"security": key[0], "date": key[1], **row["assumption"]}
            self.daily_assumptions[key] = value
            self.queries.append({"api": "execution_daily_assumption", **value})

    def component(self, sid, day):
        self.advance_day(day)
        key = sid, day
        if key not in self.components:
            self.register([sid])
            prev = self.calendar[self.calendar.index(day) - 1]
            rows = self._daily(sid, [prev, day])
            rows[0] = self._execution_row(sid, prev, rows[0])
            if rows[1] is None:
                rows[1] = carry_daily(rows[0], day)
            for row in rows:
                self._record_daily_assumption(row)
            self.execution_closes[sid] = rows[1]
            facts = None
            listings = None
            if self.owner.release.manifest.get("cmes"):
                if self._cmes_facts is None:
                    self._cmes_facts = cmes_facts(self.owner.release)
                facts = self._cmes_facts
                listings = listing_facts(self.owner.release, [sid], facts=facts,
                                         fact_query=self.source_fact, on_date=day)
            resolved_states = require_dated_states(
                rows, [day], facts=facts, fact_query=self.source_fact
            )
            if facts is None:
                listings = listing_facts(self.owner.release, [sid])
            require_listing_window(self.owner.release, [sid], [day], listings=listings, rows=rows)
            req = self.request.model_copy(
                update={
                    "securities": (sid,),
                    "start_date": date.fromisoformat(day),
                    "end_date": date.fromisoformat(day),
                    "warmup_sessions": 1,
                }
            )
            corp = self._corporate(sid, day)
            daily = {
                "rows": rows,
                "receipt": {"release": self.pin, "rows_sha256": digest(rows)},
            }
            if self.frequency == "1d":
                from ashare_quant.backtests.daily.python_inputs import DailyPythonInputs

                metadata, daily, _ = prepare_market(
                    self.pin,
                    req,
                    owner=self.owner,
                    daily=daily,
                    corporate=corp,
                    facts=facts,
                    listings=listings,
                    resolved_states=resolved_states,
                )
                doc = {
                    **metadata,
                    "schema": "aq-python-daily-input.v1",
                    "producer": {"name": "ashare-data"},
                    "daily": daily["rows"],
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
                packed = directory / "input.json.gz"
                if not packed.exists():
                    prepare(
                        self.pin,
                        req,
                        directory,
                        owner=self.owner,
                        daily=daily,
                        corporate=corp,
                        facts=facts,
                        listings=listings,
                        resolved_states=resolved_states,
                    )
                    compress(directory / "input.json")
                part = MinuteInputs(
                    packed,
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
                        "file": packed.relative_to(self.root).as_posix(),
                        "sha256": file_hash(packed),
                    },
                    len(part.bars),
                )
            self.components[key] = part
        return self.components[key]

    def row(self, sid, day):
        if day not in self.days:
            row = self._execution_row(sid, day, self._daily(sid, [day])[0])
            self._record_daily_assumption(row)
            return row
        return self.component(sid, day).row(sid, day)

    def state(self, sid, day, *, at=None):
        return self.component(sid, day).state(sid, day, at=at)

    def current(self, sid, at):
        day = str(at.date())
        if self.frequency == "1d":
            field = "close" if at.hour >= 15 or at.hour < 9 else "open"
            if at.hour * 60 + at.minute < 570:
                day, field = self.previous[day], "close"
            row = self._execution_row(sid, day, self._daily(sid, [day])[0])
            self._record_daily_assumption(row)
            return float(number(row.get(field)))
        return self.component(sid, day).current(sid, at)

    valuation_price = current

    def quote_price(self, sid, at):
        """A read-only last price does not prepare a native execution component."""
        if self.frequency == "1d":
            return self.current(sid, at)
        day = str(at.date())
        if at.strftime("%H:%M") < "09:31":
            previous = self.previous[day]
            row = self._execution_row(sid, previous, self._daily(sid, [previous])[0])
            self._record_daily_assumption(row)
            return float(number(row["close"]))
        daily = self._daily(sid, [day])[0]
        if daily is None:
            row = self._execution_row(sid, day)
            self._record_daily_assumption(row)
            return float(number(row["close"]))
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
        from ashare_quant.tasks.benchmark_comparison import release_security_names

        return {
            "ready": True,
            "data_access": "demand.v2",
            "frequency": self.frequency,
            "securities": list(self.ids),
            "security_names": release_security_names(self.owner.release, self.ids),
            "trading_dates": self.days,
            "daily_rows": self.daily_read_rows,
            "assumed_daily_rows": len(self.daily_assumptions),
            "daily_assumptions": list(self.daily_assumptions.values()),
            "daily_row_count_basis": "actual rows read, including evicted-window rereads",
            "history_factors": self.document["history_factors"],
            "corporate_schedule_sha256": file_hash(self.root / "corporate.jsonl.gz"),
            "historical_pit_verified": False,
            "state_visibility": self.state_visibility,
            "resource_budget": self.policy.model_dump(mode="json"),
            "limitations": self.document["limitations"],
        }

    def finalize(self):
        self.document.update(
            securities=list(self.ids),
            evidence=[journal.seal() for journal in
                      (self.reads, self.queries, self.corporate_evidence)],
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
        for journal in (self.reads, self.queries, self.corporate_evidence):
            journal.close()
        if self.references is not None:
            self.references.close()
            self.references = None
