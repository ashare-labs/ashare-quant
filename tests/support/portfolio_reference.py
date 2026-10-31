"""Primary raw data and order observations; missing evidence never means alignment.

Snapshots bound observation times, not exchange event times. Pair order IDs by
actual submit/return sequence, never by numeric coincidence.
"""

import datetime as dt
import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

TOL = Decimal("0.000001")
SHANGHAI = timezone(timedelta(hours=8))
STATUS = {
    "held": "FILLED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "open": "ACTIVE",
    "new": "PENDING_NEW",
}
ORDER_FIELDS = ("security", "side", "quantity", "filled", "remaining", "status")
RAW_FIELDS = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "money": "money",
    "paused": "suspended",
    "high_limit": "upper_limit",
    "low_limit": "lower_limit",
}


def primary_audit(events, aq, owner, spec):
    differences, gaps = [], []

    def diff(code, where, **values):
        differences.append(dict(code=code, where=where, **values))

    def gap(code, where, **values):
        gaps.append(dict(code=code, where=where, **values))

    def need(obj, key, where):
        value = obj.get(key) if isinstance(obj, dict) else None
        if value is None or isinstance(value, dict):
            gap("MISSING_FIELD", where, field=key)
            return None
        return value

    def items(obj, key, where):
        value = need(obj, key, where)
        if value is None:
            return []
        if not isinstance(value, list):
            diff("EXPECTED_LIST", where, field=key)
            return []
        return value

    def number(value, where, *, integer=False):
        if value is None:
            return None
        try:
            n = Decimal(str(value))
            if isinstance(value, bool) or not n.is_finite() or (integer and n != int(n)):
                raise ValueError
            return int(n) if integer else n
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            diff("INVALID_NUMBER", where, value=value)
            return None

    def clock(value, where):
        if value is None:
            gap("TIME_MISSING", where)
            return None
        try:
            d = datetime.fromisoformat(str(value))
            d = d.replace(tzinfo=SHANGHAI) if d.tzinfo is None else d.astimezone(SHANGHAI)
            return d.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            diff("INVALID_TIME", where, value=value)
            return None

    def equal(a, b, code, where, *, numeric=False, exact=False):
        if a is None or b is None:
            return
        if numeric:
            a, b = number(a, where + "/aq"), number(b, where + "/jq")
            if a is None or b is None:
                return
            same = a == b if exact else abs(a - b) <= TOL
        else:
            same = a == b
        if not same:
            diff(
                code,
                where,
                aq=sorted(a) if isinstance(a, set) else a,
                jq=sorted(b) if isinstance(b, set) else b,
            )

    def order(obj, jq, where):
        if not isinstance(obj, dict):
            gap("ORDER_OBJECT_MISSING", where)
            return None
        oid = need(obj, "order_id" if jq else "id", where)
        sid = need(obj, "security", where)
        side = need(obj, "is_buy" if jq else "side", where)
        if jq:
            if type(side) is not bool:
                if side is not None:
                    diff("INVALID_ORDER_SIDE", where, value=side)
                side = None
            else:
                side = "BUY" if side else "SELL"
        elif side not in {"BUY", "SELL", None}:
            diff("INVALID_ORDER_SIDE", where, value=side)
        q = number(
            need(obj, "amount" if jq else "quantity", where), where + "/quantity", integer=True
        )
        f = number(need(obj, "filled", where), where + "/filled", integer=True)
        status = need(obj, "status", where)
        status = STATUS.get(str(status).lower(), "UNKNOWN") if jq and status is not None else status
        if status not in {"FILLED", "CANCELLED", "REJECTED", "ACTIVE", "PENDING_NEW", None}:
            diff("UNKNOWN_ORDER_STATUS", where, raw=obj.get("status"))
        if q is not None and f is not None and (q <= 0 or not 0 <= f <= q):
            diff("INVALID_ORDER_QUANTITIES", where, quantity=q, filled=f)
        if status == "FILLED" and q is not None and f != q:
            diff("FILLED_STATUS_WITH_REMAINDER", where, quantity=q, filled=f)
        return dict(
            id=str(oid) if oid is not None else None,
            security=sid,
            side=side,
            quantity=q,
            filled=f,
            remaining=q - f if q is not None and f is not None else None,
            status=status,
        )

    def compare_order(a, b, where):
        if a is not None and b is not None:
            for field in ORDER_FIELDS:
                equal(a[field], b[field], "ORDER_" + field.upper(), where + "/" + field)

    ids = spec.get("securities", [])
    days = [a["date"] for a in aq.get("accounts", [])]
    market = {(r["date"], r["security"]): r for r in owner.get("daily", [])}
    raw, snapshots, submissions, returns, trades, histories = {}, {}, [], [], {}, {}
    previous_time, pending = None, None
    terminal_observations = []
    for index, e in enumerate(events):
        where = f"jq/events/{index}"
        at = clock(need(e, "time", where), where)
        day = at[:10] if at else None
        if previous_time and at and at < previous_time:
            diff("EVENT_TIME_REGRESSION", where, previous=previous_time, current=at)
        previous_time = at or previous_time
        kind = e.get("kind")
        if kind == "daily_raw":
            sid = need(e, "security", where)
            key = day, sid
            if key in raw:
                diff("DUPLICATE_DAILY_RAW", where, key=key)
            records = items(e, "records", where)
            if len(records) != 1:
                gap("DAILY_RAW_ROW_COUNT", where, count=len(records), expected=1)
            if not records or not isinstance(records[0], dict):
                gap("DAILY_RAW_RECORD_MISSING", where)
                continue
            record = records[0]
            raw[key] = record
            date = record.get("date", record.get("index"))
            row_at = clock(date, where + "/row-date")
            if row_at and row_at[:10] != day:
                diff("RAW_DATE_MISMATCH", where, event_date=day, row_date=date)
            if day not in days or sid not in ids:
                diff("RAW_OUT_OF_SCOPE", where, date=day, security=sid)
                continue
            source = market.get(key)
            if source is None:
                gap("OWNER_RAW_ROW_MISSING", where, key=key)
                continue
            for jf, af in RAW_FIELDS.items():
                jv, av = need(record, jf, where), need(source, af, "owner/" + str(key))
                if jf == "paused":
                    if jv not in (None, True, False, 0, 1):
                        diff("INVALID_PAUSED_FLAG", where, value=jv)
                    equal(av, bool(jv) if jv is not None else None, "RAW_FIELD", where + "/paused")
                else:
                    jv = number(jv, where + "/" + jf, integer=jf == "volume")
                    if jv is not None and (jv < 0 or (jf not in {"volume", "money"} and jv == 0)):
                        diff("INVALID_RAW_VALUE", where, field=jf, value=jv)
                    equal(av, jv, "RAW_FIELD", where + "/" + jf, numeric=True, exact=jf == "volume")
        if kind == "submit":
            if pending is not None:
                gap("ORDER_RETURN_MISSING", where, previous_submit=pending)
            submissions.append(e)
            returns.append(None)
            pending = len(submissions) - 1
        elif kind == "order_return":
            if pending is None:
                diff("ORDER_RETURN_WITHOUT_SUBMIT", where)
            else:
                equal(
                    submissions[pending].get("security"),
                    e.get("security"),
                    "RETURN_SECURITY",
                    where,
                )
                equal(submissions[pending].get("time"), e.get("time"), "RETURN_CLOCK", where)
                returns[pending], pending = e, None
        if kind == "snapshot":
            key = day, e.get("label")
            if key in snapshots:
                diff("DUPLICATE_SNAPSHOT", where, key=key)
            snapshots[key] = e
            seen = set()
            for t in items(e, "trades", where):
                tid = need(t, "trade_id", where)
                if tid is None:
                    continue
                tid = str(tid)
                if tid in seen:
                    diff("DUPLICATE_SNAPSHOT_TRADE", where, trade=tid)
                seen.add(tid)
                if tid in trades and trades[tid] != t:
                    diff("TRADE_CHANGED", where, trade=tid)
                trades.setdefault(tid, t)
        observed = items(e, "orders", where) if kind == "snapshot" else []
        if kind == "order_return":
            returned_none = need(e, "returned_none", where)
            if type(returned_none) is not bool and returned_none is not None:
                diff("INVALID_RETURNED_NONE", where)
            if returned_none is False:
                observed = [e.get("order")]
            elif returned_none is True and e.get("order") is not None:
                diff("NONE_RETURN_HAS_ORDER", where)
        current_orders = {}
        for obj in observed:
            o = order(obj, True, where)
            if o is None or o["id"] is None:
                continue
            oid = o["id"]
            if oid in current_orders:
                diff("DUPLICATE_SNAPSHOT_ORDER", where, order=oid)
            current_orders[oid] = o
            prior = histories.get(oid)
            if prior:
                for f in ("security", "side", "quantity"):
                    equal(prior[f], o[f], "ORDER_IMMUTABLE_CHANGED", where + "/" + f)
                if (
                    prior["filled"] is not None
                    and o["filled"] is not None
                    and o["filled"] < prior["filled"]
                ):
                    diff("ORDER_FILL_REGRESSED", where, order=oid)
                if prior["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
                    equal(prior["status"], o["status"], "TERMINAL_STATUS_CHANGED", where)
            histories[oid] = o
            if o["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
                terminal_observations.append(
                    dict(order=oid, observed_by=at, label=e.get("label", kind), status=o["status"])
                )
            if kind == "snapshot" and o["filled"] is not None:
                fills = [t for t in e.get("trades", []) if str(t.get("order_id")) == oid]
                amounts = [number(need(t, "amount", where), where, integer=True) for t in fills]
                if all(q is not None for q in amounts):
                    equal(o["filled"], sum(amounts), "ORDER_TRADE_FILLED_SUM", where + "/" + oid)
        if kind == "snapshot":
            opened = [str(x) for x in items(e, "open_orders", where)]
            if len(opened) != len(set(opened)):
                diff("DUPLICATE_OPEN_ORDER", where)
            expected = {oid for oid, o in current_orders.items() if o["status"] == "ACTIVE"}
            if set(opened) != expected:
                diff(
                    "OPEN_ORDERS_INCONSISTENT",
                    where,
                    reported=opened,
                    observed_active=sorted(expected),
                )
    if pending is not None:
        gap("ORDER_RETURN_MISSING", "jq/end", submit=pending)
    for day in days:
        for sid in ids:
            if (day, sid) not in raw:
                gap("DAILY_RAW_MISSING", "jq/daily_raw", date=day, security=sid)

    local_submissions = aq.get("submissions", [])
    mapping = {}
    equal(len(local_submissions), len(submissions), "SUBMISSION_COUNT", "submissions")
    for i, (a, j, returned) in enumerate(zip(local_submissions, submissions, returns)):
        where = f"submissions/{i}"
        equal(a.get("security"), need(j, "security", where), "SUBMIT_SECURITY", where)
        equal(
            a.get("quantity"),
            number(need(j, "quantity", where), where, integer=True),
            "SUBMIT_QUANTITY",
            where,
        )
        equal(
            clock(a.get("at"), "aq/" + where),
            clock(j.get("time"), "jq/" + where),
            "SUBMIT_TIME",
            where,
        )
        equal(
            a.get("cash_before"), need(j, "cash_before", where), "SUBMIT_CASH", where, numeric=True
        )
        if returned is None:
            continue
        equal(a.get("returned_none"), returned.get("returned_none"), "ORDER_RETURN_NONE", where)
        equal(
            a.get("cash_after"),
            need(returned, "cash_after", where),
            "RETURN_CASH",
            where,
            numeric=True,
        )
        if returned.get("returned_none") is False:
            jo = order(returned.get("order"), True, "jq/" + where)
            ao = order(a.get("returned_order"), False, "aq/" + where)
            compare_order(ao, jo, where + "/return")
            if jo and jo["id"] is not None and a.get("order_id") is not None:
                if jo["id"] in mapping:
                    diff("ORDER_REUSED_ACROSS_SUBMISSIONS", where, order=jo["id"])
                mapping[jo["id"]] = str(a["order_id"])
    local_orders = {str(o["id"]): order(o, False, "aq/final_orders") for o in aq.get("orders", [])}
    for oid, jo in histories.items():
        ao = local_orders.get(mapping.get(oid))
        if ao is None:
            diff("UNPAIRED_JQ_ORDER", "final_orders", order=oid)
        compare_order(ao, jo, "final_orders/" + oid)
    for oid in local_orders:
        if oid not in mapping.values():
            diff("UNPAIRED_AQ_ORDER", "final_orders", order=oid)

    local_snapshots = {(o["at"][:10], o["label"]): o for o in aq.get("observations", [])}
    required = {(d, "day_close") for d in days}
    required.update(
        (d["date"], label)
        for d in aq.get("decisions", [])
        if d.get("rebalance")
        for label in ("before_orders", "after_orders")
    )
    for key in sorted(required):
        a, j = local_snapshots.get(key), snapshots.get(key)
        where = "snapshots/" + str(key)
        if a is None or j is None:
            gap(
                "LIFECYCLE_SNAPSHOT_MISSING",
                where,
                aq_present=a is not None,
                jq_present=j is not None,
            )
            continue
        equal(
            clock(a.get("at"), "aq/" + where),
            clock(j.get("time"), "jq/" + where),
            "SNAPSHOT_TIME",
            where,
        )
        aa, ja = a.get("account", {}), j.get("account", {})
        for af, jf in (("cash", "available_cash"), ("equity", "total_value")):
            equal(
                need(aa, af, "aq/" + where),
                need(ja, jf, "jq/" + where),
                "SNAPSHOT_ACCOUNT",
                where + "/" + af,
                numeric=True,
            )
        equal(
            need(a, "frozen_cash", "aq/" + where),
            need(ja, "locked_cash", "jq/" + where),
            "FROZEN_CASH",
            where,
            numeric=True,
        )
        ao = {str(o["id"]): order(o, False, "aq/" + where) for o in a.get("orders", [])}
        jo = [order(o, True, "jq/" + where) for o in j.get("orders", [])]
        equal(
            set(ao),
            {mapping.get(o["id"], "unpaired:" + str(o["id"])) for o in jo if o},
            "SNAPSHOT_ORDER_SET",
            where,
        )
        for o in jo:
            if o:
                compare_order(ao.get(mapping.get(o["id"])), o, where + "/" + str(o["id"]))
        opened = items(j, "open_orders", "jq/" + where)
        equal(
            set(items(a, "open_order_ids", "aq/" + where)),
            {mapping.get(str(oid), "unpaired:" + str(oid)) for oid in opened},
            "OPEN_ORDER_SET",
            where,
        )

    jq_sequence = []
    for tid, t in trades.items():
        where = "jq/trades/" + tid
        oid = str(need(t, "order_id", where))
        o = histories.get(oid)
        if o is None:
            gap("TRADE_ORDER_MISSING", where, order=oid)
        jq_sequence.append(
            dict(
                order_id=mapping.get(oid),
                security=need(t, "security", where),
                side=o["side"] if o else None,
                at=clock(need(t, "time", where), where),
                quantity=number(need(t, "amount", where), where, integer=True),
                price=number(need(t, "price", where), where),
            )
        )
    equal(len(aq.get("trades", [])), len(jq_sequence), "TRADE_COUNT", "trades")
    for i, (a, j) in enumerate(zip(aq.get("trades", []), jq_sequence)):
        for f in ("order_id", "security", "side", "quantity", "price", "at"):
            av = clock(a.get(f), f"aq/trades/{i}") if f == "at" else need(a, f, f"aq/trades/{i}")
            equal(av, j[f], "TRADE_SEQUENCE_OR_TIME", f"trades/{i}/{f}", numeric=f == "price")

    ratio = spec.get("execution", {}).get("volume_ratio")
    if spec.get("expansion_case"):
        if "volume_ratio" not in events[0]:
            gap("PARTICIPATION_PARAMETER_MISSING", "jq/identity")
        elif events[0]["volume_ratio"] != ratio:
            diff("PARTICIPATION_PARAMETER", "jq/identity", aq=ratio, jq=events[0]["volume_ratio"])
    cap_checks = []
    if ratio is not None:
        for label, seq in (("aq", aq.get("trades", [])), ("jq", jq_sequence)):
            used = {}
            for t in seq:
                if t.get("at") is None or t.get("quantity") is None:
                    continue
                key = t["at"][:10], t["security"]
                row = market.get(key, {}) if label == "aq" else raw.get(key, {})
                volume = number(need(row, "volume", label + "/cap/" + str(key)), label + "/cap")
                if volume is None:
                    continue
                with localcontext(prec=50):
                    cap = (
                        int((volume * Decimal(str(ratio)) / 100).to_integral_value(ROUND_FLOOR))
                        * 100
                    )
                used[key] = used.get(key, 0) + t["quantity"]
                cap_checks.append(
                    dict(side=label, date=key[0], security=key[1], cap=cap, cumulative=used[key])
                )
                if used[key] > cap:
                    diff(
                        "STRICT_PARTICIPATION_CAP",
                        label + "/cap/" + str(key),
                        cap=cap,
                        filled=used[key],
                    )

    contract = []
    if "session end" in spec.get("execution", {}).get("unfilled", "").lower():
        for o in aq.get("orders", []):
            if o.get("status") != "CANCELLED" or o.get("filled", 0) >= o.get("quantity", 0):
                continue
            traces = [
                e
                for e in aq.get("order_events", [])
                if e.get("order", {}).get("id") == o["id"]
                and e["order"].get("status") == "CANCELLED"
            ]
            if not traces:
                gap("CANCELLATION_EVENT_MISSING", "aq/order/" + o["id"])
            elif traces[0].get("phase") == "EXECUTE":
                contract.append(
                    dict(
                        side="aq",
                        order=o["id"],
                        code="CANCELLED_BEFORE_SESSION_END",
                        at=traces[0]["at"],
                    )
                )
        for obs in terminal_observations:
            if obs["status"] == "CANCELLED" and obs["label"] in {"order_return", "after_orders"}:
                contract.append(dict(side="jq", code="CANCELLED_BEFORE_SESSION_END", **obs))

    return dict(
        differences=differences,
        evidence_gaps=gaps,
        contract_differences=contract,
        evidence_complete=not gaps,
        eligible_for_alignment=not (differences or gaps or contract),
        order_id_mapping=mapping,
        terminal_observations=terminal_observations,
        participation_checks=cap_checks,
        counts=dict(raw_rows=len(raw), orders=len(histories), trades=len(trades)),
        timing_scope="JQ observation upper bounds and same-label snapshots; no invented cancellation timestamp",
    )


def generate(manifest, constant=False):
    cal = []
    day = dt.date(2019, 7, 1)
    while day <= dt.date(2020, 5, 6):
        if day.weekday() < 5:
            cal.append(str(day))
        day += dt.timedelta(days=1)
    rows = []
    for j, sid in enumerate(manifest["securities"]):
        for n, day in enumerate(cal):
            close = (
                10
                if constant
                else round((10 + 5 * j) * (1 + 0.0005 * n + 0.04 * math.sin((n + j * 7) / 12)), 2)
            )
            rows.append(
                dict(
                    security=sid,
                    date=day,
                    open=str(close),
                    close=str(close),
                    high=str(round(close * 1.01, 2)),
                    low=str(round(close * 0.99, 2)),
                    volume="1000000",
                    suspended=sid == "000001.XSHE" and day == "2020-02-03",
                    upper_limit=str(round(close * 1.1, 2)),
                    lower_limit=str(round(close * 0.9, 2)),
                )
            )
    return dict(
        schema="ad-daily-portfolio.v1",
        producer={"name": "synthetic-test", "version": "1"},
        real_market_data=False,
        source_refs=["synthetic weekdays and deterministic prices"],
        limitations=["synthetic; not exchange observations or JQ comparison"],
        price_basis="raw",
        volume_unit="share",
        securities=manifest["securities"],
        calendar=cal,
        daily=rows,
        actions=[],
        action_coverage={"status": "synthetic_known"},
        listing_coverage="synthetic_known",
    )
