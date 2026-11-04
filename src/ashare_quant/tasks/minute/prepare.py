"""Prepare bounded, source-labelled research input from one pinned formal release.

Originals are read-only. Time intervals and daily replay remain explicit models;
source labels outside those model sessions are counted, never deleted upstream.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ashare_data import export_minute_research
from ashare_data.product.market_release import ReleasedDailyProduct, minute_prices
from ashare_data.product.released_corporate import ReleasedCorporateView
from ashare_data.product.released_references import ReleasedReferences
from ashare_data.storage.market_release import MarketRelease

from ashare_quant.backtests.minute.research import (
    CORPORATE_ASSUMPTIONS,
    CORPORATE_PROFILE,
    LIMIT_MODEL,
    digest,
)
from ashare_quant.contracts import DomainError
from ashare_quant.portfolio.corporate import MODEL
from ashare_quant.storage.artifacts import atomic
from ashare_quant.tasks.minute.snapshot import IndexedQueries


def require(condition, code, message, details=None):
    if not condition:
        raise DomainError(code, message, 422, details=details)


def scope(pin, ids, start, end, frequency="1m", warmup_sessions=20, *, release=None):
    release = release or MarketRelease(
        pin["root"], pin["release_id"], expected_manifest_sha256=pin["manifest_sha256"]
    )
    with release.metadata() as db:
        calendar = [r[0] for r in db.execute("SELECT date FROM calendar ORDER BY date")]
    days = [d for d in calendar if start <= d <= end]
    require(
        days and calendar.index(days[0]) > 0 and calendar.index(days[-1]) < len(calendar) - 1,
        "PYTHON_CALENDAR_GAP",
        "Requested dates need trading days, a previous and a following session",
    )
    if frequency == "1d":
        require(
            calendar.index(days[0]) >= warmup_sessions,
            "PYTHON_WARMUP_GAP",
            "The release has insufficient requested daily warmup sessions",
        )
        return days, calendar[
            calendar.index(days[0]) - warmup_sessions : calendar.index(days[-1]) + 2
        ]
    return days, calendar[calendar.index(days[0]) - 1 : calendar.index(days[-1]) + 2]


def cmes_facts(release):
    """Select the pin-bound partial reader only for a CMES publication."""
    if not release.manifest.get("cmes"):
        return None
    from ashare_data.product.released_cmes_facts import ReleasedCmesFacts

    return ReleasedCmesFacts(release)


def query_cmes_fact(reader, method, *arguments):
    from ashare_data.core.model import DataError

    try:
        return getattr(reader, method)(*arguments)
    except DataError as exc:
        raise DomainError(exc.code, exc.message, 422, details=exc.details,
                          origin="ashare-data") from exc


def listing_facts(release, ids, *, facts=None, fact_query=None, on_date=None):
    """Read listing event dates; CMES events do not establish active intervals."""
    facts = facts if facts is not None else cmes_facts(release)
    if facts is not None:
        query = fact_query or (lambda method, *args: query_cmes_fact(facts, method, *args))
        listings = {}
        for sid in ids:
            fact = query("listing_date", sid)
            value = fact.get("value")
            try:
                valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
            require(valid and fact.get("status") == "source_observed",
                    "PYTHON_LISTING_GAP", "Missing or conflicting source-reported listing date",
                    {"security": sid, "field": "source_reported_listing_date", "fact": fact})
            if on_date is not None:
                require(date.fromisoformat(value) <= date.fromisoformat(on_date) - timedelta(days=30),
                        "MINUTE_RESEARCH_RECENT_LISTING_UNSUPPORTED",
                        "The source-reported listing event must be at least 30 days before execution",
                        {"security": sid, "date": on_date, "fact": fact})
            listings[sid] = {"ipo": value, "source_fact": fact}
        return listings
    listings = {}
    with release.metadata() as db:
        for sid in ids:
            symbol = ("sh" if sid.endswith("XSHG") else "sz") + sid[:6]
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM listing_facts WHERE symbol=? ORDER BY observed_at DESC",
                    (symbol,),
                )
            ]
            require(
                rows and len({r["ipo"] for r in rows}) == 1,
                "PYTHON_LISTING_GAP",
                "Missing or conflicting published execution listing evidence",
                {"security": sid},
            )
            listings[sid] = rows[0]
    return listings


def _positive_trading_bar(row):
    if not isinstance(row, dict):
        return False
    try:
        op, hi, lo, cl = (Decimal(str(row[k])) for k in ("open", "high", "low", "close"))
        vol, amt = Decimal(str(row["volume"])), Decimal(str(row["amount"]))
        return (
            all(v.is_finite() and v > 0 for v in (op, hi, lo, cl))
            and lo <= min(op, cl) <= max(op, cl) <= hi
            and vol.is_finite()
            and amt.is_finite()
            and vol > 0
            and amt > 0
        )
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return False


def require_dated_states(rows, days, *, facts=None, fact_query=None):
    """Keep raw trading status separate from independently evidenced ST facts."""
    resolved = {}
    for row in rows:
        if row["date"] not in days:
            continue  # Warmup close needs no state; pause-filtered history checks it on demand.
        st = None
        if facts is not None:
            query = fact_query or (lambda method, *args: query_cmes_fact(facts, method, *args))
            st = query("st_status", row["security"], row["date"])
        st_known = (st is not None and type(st.get("value")) is bool
                    and st.get("status") == "derived")
        if st_known and row.get("is_st") in {"0", "1"}:
            require(st["value"] == (row["is_st"] == "1"), "PYTHON_STATE_CONFLICT",
                    "Published raw ST status conflicts with source-scoped membership",
                    {"security": row["security"], "date": row["date"], "fact": st})
        ts_known = row.get("tradestatus") in {"0", "1"} or (
            facts is not None and _positive_trading_bar(row)
        )
        missing = []
        if not ts_known:
            missing.append("tradestatus")
        if row.get("is_st") not in {"0", "1"} and not st_known:
            missing.append("is_st")
        require(
            not missing,
            "PYTHON_STATE_GAP",
            "Missing dated trading/ST status",
            {"security": row["security"], "date": row["date"],
             "fields": missing, **({"source_facts": {"is_st": st}} if st is not None else {})},
        )
        resolved[row["security"], row["date"]] = {
            "tradestatus": row["tradestatus"] if row.get("tradestatus") in {"0", "1"} else "1",
            "is_st": row["is_st"] if row.get("is_st") in {"0", "1"} else ("1" if st["value"] else "0"),
        }
    return resolved


def require_listing_window(release, ids, days, *, listings=None, rows=None):
    """A company event date requires verified positive trading activity across execution days."""
    if not release.manifest.get("cmes"):
        return
    bykey = {(r["security"], r["date"]): r for r in (rows or ())}
    verified = (
        listings is not None
        and all(
            sid in listings
            and isinstance(listings[sid].get("ipo"), str)
            and all(
                _positive_trading_bar(bykey.get((sid, day)))
                and date.fromisoformat(listings[sid]["ipo"])
                <= date.fromisoformat(day) - timedelta(days=30)
                for day in days
            )
            for sid in ids
        )
    )
    require(verified, "PYTHON_LISTING_STATE_GAP",
            "CMES listing event and ST facts do not establish historical active eligibility",
            {"securities": list(ids), "dates": list(days),
             "fields": ["historical_listing_status"], "release": release.receipt()})


def prepare_market(pin, request, *, owner=None, daily=None, corporate=None):
    """Shared source observations for both Python frequencies; no execution or collection."""
    ids = list(request.securities)
    owner = owner or ReleasedDailyProduct({"market_release": pin})
    days, cal = scope(
        pin,
        ids,
        str(request.start_date),
        str(request.end_date),
        request.frequency,
        request.warmup_sessions,
        release=owner.release,
    )
    calendar = cal
    release = owner.release
    facts = cmes_facts(release)
    daily = daily if daily is not None else owner.prices(ids, cal[0], days[-1])
    bykey = {(r["security"], r["date"]): dict(r) for r in daily["rows"]}
    missing = [
        {"security": sid, "date": day} for sid in ids for day in cal[:-1] if (sid, day) not in bykey
    ]
    require(
        not missing,
        "PYTHON_DAILY_GAPS",
        "Daily observations are missing",
        {"missing": missing[:50]},
    )
    listings = listing_facts(release, ids, facts=facts, on_date=days[0])
    resolved_states = require_dated_states(bykey.values(), days, facts=facts)
    require_listing_window(release, ids, days, listings=listings, rows=bykey.values())
    for (sid, day), row in bykey.items():
        if day not in days:
            continue  # Only execution days contribute state observations.
        if (sid, day) in resolved_states:
            row["tradestatus"] = resolved_states[sid, day]["tradestatus"]
            row["is_st"] = resolved_states[sid, day]["is_st"]
        row["suspended"] = row["tradestatus"] == "0"
    daily = {
        **daily,
        "rows": [
            {**r, **resolved_states.get((r["security"], r["date"]), {})}
            for r in daily["rows"]
        ],
    }
    reader = ReleasedReferences(pin, pin) if corporate is None else None
    try:
        corporate_start = days[0] if request.frequency == "1d" else cal[0]
        view = ReleasedCorporateView(reader, ids, corporate_start, days[-1]) if reader else None
        corp = corporate if corporate is not None else view.export_aq(
            ids,
            start=corporate_start,
            end=days[-1],
            tax_model="gross_cash_no_tax",
            accept_source_scoped_coverage=True,
        )
        require(
            corp["ready_for_declared_date_model"],
            "PYTHON_CORPORATE_GAPS",
            "Corporate evidence cannot support this window",
            {"issues": corp["issues"]},
        )
    finally:
        if reader:
            reader.close()
    refs = {
        "market": {"source": "Frozen AD MarketRelease", "sha256": release.sha256},
        "daily": {
            "source": "FrozenDailyStore raw daily response, dated states and explicit preclose model",
            "sha256": digest(daily),
        },
        "listing": {
            "source": "Frozen release listing_facts",
            "sha256": digest(listings),
        },
        "corporate": {
            "source": "Frozen release source-observed corporate schedule",
            "sha256": digest(corp),
        },
    }
    start, end = map(date.fromisoformat, (cal[0], cal[-1]))
    dates = {
        (start + timedelta(days=n)).isoformat(): (start + timedelta(days=n)).isoformat() in cal
        for n in range((end - start).days + 1)
    }
    sessions = {
        d: [
            [d + "T09:30:00+08:00", d + "T11:30:00+08:00"],
            [d + "T13:00:00+08:00", d + "T15:00:00+08:00"],
        ]
        for d in days
    }
    metadata = dict(
        real_market_data=True,
        securities=ids,
        calendar=cal,
        days=days,
        sessions=sessions,
        price_basis="raw",
        volume_unit="share",
        amount_unit="CNY",
        visibility="assumed_closed",
        state_visibility="posthoc_daily_replay",
        limitations=[
            "Continuous native account; source labels under explicit retrospective clock, non-PIT",
            "5%/10% daily-preclose model is not certified exchange limits",
            "Source-scoped gross cash dividends, no investor tax or all-type action completeness claim",
            "Frozen original universe, no selection or tuning on returns",
        ],
        state_replay=dict(
            model="dated-daily-state-0900.v1",
            purpose="retrospective_research",
            evidence=["daily"],
            limitations=["Daily source states replayed at 09:00; availability unknown"],
        ),
        source_refs={"evidence": refs},
        gap_policy="explicit_unavailable",
        states=[],
        actions=[],
        research=dict(
            profile=CORPORATE_PROFILE,
            purpose="retrospective_research",
            assumptions=CORPORATE_ASSUMPTIONS,
            strict_execution_admitted=False,
            interval_verified=False,
            price_limit_verified=False,
            historical_pit=False,
            envelope=LIMIT_MODEL,
            capabilities=dict(
                matching="next_open",
                lifecycle="closed_bars",
                capacity_policy="strict_minute",
                initial_positions="flat",
                terminal_positions="flat",
                corporate_actions="source_scoped_cash_dates",
            ),
            calendars={
                x: dict(days=dates, source_refs=["market"]) for x in {s.split(".")[1] for s in ids}
            },
        ),
        corporate_schedule=corp,
        corporate_account_model=MODEL,
        action_coverage=dict(
            status="source_scoped_corporate_date_model",
            complete=False,
            absence_proved=False,
            historical_pit=False,
            binding_sha256=corp["binding_sha256"],
            response_sha256=digest(corp),
            evidence=["corporate"],
        ),
    )
    for sid in ids:
        for day in days:
            previous = calendar[calendar.index(day) - 1]
            row, prev = bykey[sid, day], bykey[sid, previous]
            raw = {
                k: v
                for k, v in row.items()
                if k
                not in {
                    "security",
                    "suspended",
                    "upper_limit",
                    "lower_limit",
                    "limit_origin",
                }
            }
            raw["symbol"] = ("sh" if sid.endswith("XSHG") else "sz") + sid[:6]
            facts = dict(
                listed=True,
                suspended=row["suspended"],
                is_st={"0": False, "1": True}.get(row["is_st"]),
                previous_close=prev["close"],
                first_listing_date=listings[sid]["ipo"],
            )
            metadata["states"].append(
                dict(
                    security=sid,
                    date=day,
                    **facts,
                    reference_price=None,
                    upper_limit=None,
                    lower_limit=None,
                    available_at=None,
                    historical_available_at=None,
                    observed_at=row["observed_at"],
                    replay_effective_at=day + "T09:00:00+08:00",
                    evidence={"source_refs": ["daily", "listing"]},
                    source_observation=dict(
                        security=sid,
                        date=day,
                        historical_available_at=None,
                        intraday_halts_verified=False,
                        dependency_conflicts=[],
                        source_quality=dict(
                            execution_permission=False,
                            limitations=["Uncertified dated source observations"],
                        ),
                        previous_session=previous,
                        **{
                            k: dict(
                                value=v,
                                status="source_observed",
                                source_refs=[
                                    "listing" if k in {"listed", "first_listing_date"} else "daily"
                                ],
                            )
                            for k, v in facts.items()
                        },
                        **{
                            k: dict(value=None)
                            for k in ("reference_price", "upper_limit", "lower_limit")
                        },
                    ),
                    previous_close_observation=dict(
                        security=sid,
                        date=previous,
                        close=prev["close"],
                        price_basis="raw",
                        source_refs=["daily"],
                    ),
                    daily_limit_model=dict(
                        schema=LIMIT_MODEL,
                        source_row=raw,
                        source_row_sha256=digest(raw),
                        source_refs=["daily"],
                        exchange_limits_verified=False,
                    ),
                )
            )
    return metadata, daily, release


def prepare(pin, request, root, history_factors=None, *, owner=None, corporate=None):
    metadata, daily, release = prepare_market(pin, request, owner=owner, corporate=corporate)
    if request.frequency == "1d":
        from ashare_quant.backtests.daily.python_inputs import prepare_daily

        return prepare_daily(metadata, daily, release, request, root, history_factors)
    ids, days, sessions = metadata["securities"], metadata["days"], metadata["sessions"]
    bykey = {(r["security"], r["date"]): r for r in daily["rows"]}
    cfg = {"market_release": pin}
    bundles, reads = [], []
    excluded, unavailable = [], []
    annotations = {}
    for sid in ids:
        for offset in range(0, len(days), 50):
            query_days = days[offset : offset + 50]
            response = minute_prices(
                cfg,
                sid,
                query_days[0] + "T09:30:00+08:00",
                query_days[-1] + "T15:00:00+08:00",
                release=release,
            )
            sha = digest(response)
            source = dict(
                id=f"release-{release.release_id}-{sid}-{query_days[0]}-{query_days[-1]}",
                kind="local_observation",
                location="immutable bounded market release query",
                observed_at=max(bykey[sid, d]["observed_at"] for d in days),
                label="end",
                time_semantics="unverified",
                time_evidence="Source label retained; declared closed-bar interval is a model",
                volume_unit="shares",
                amount_unit="CNY",
                price_unit="CNY",
                price_basis="unadjusted",
                unit_evidence="Unmodified release values and quality denials",
                source_sha256=sha,
            )
            rows, missing = [], []
            for index, original in enumerate(response["rows"]):
                r = dict(original)
                r["release_label_at"] = r["source_label"]
                r["source_label"] = (
                    datetime.fromisoformat(r["source_label"])
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .isoformat()
                )
                if r["trade_date"] not in days or not any(
                    x < r["source_label"] <= y for x, y in sessions[r["trade_date"]]
                ):
                    excluded.append(
                        {
                            "security": sid,
                            "source_label": r["source_label"],
                            "reason": "outside_declared_closed_bar_sessions",
                        }
                    )
                    continue
                if r.get("missing_fields"):
                    require(
                        r.get("missing_reason") == "confirmed_invalid_price"
                        and set(r["missing_fields"])
                        == {"open", "high", "low", "close", "volume", "amount"}
                        and all(r[k] is None for k in r["missing_fields"]),
                        "PYTHON_PARTIAL_MINUTE_UNAVAILABLE",
                        "This minute has missing activity fields; the current engine cannot accept a partial bar",
                        {
                            "security": sid,
                            "source_label": r["source_label"],
                            "missing_fields": r["missing_fields"],
                            "reason": r.get("missing_reason"),
                        },
                    )
                    missing.append(r)
                    unavailable.append(r)
                    continue
                native = dict(
                    r,
                    source_file_sha256=sha,
                    source_row=index,
                    source_path="bounded-api-response:" + source["id"],
                )
                bar = dict(
                    symbol=sid,
                    timestamp=r["source_label"],
                    **{k: r[k] for k in ("open", "high", "low", "close", "volume", "amount")},
                    quality="unverified",
                    quality_reason="Native source value retained",
                    available_at=None,
                    native_observation=native,
                )
                rows.append(bar)
                if (
                    r["volume"] == 0
                    and r["amount"] == 0
                    and "14:57" < r["source_label"][11:16] <= "15:00"
                ):
                    annotations[sid + "|" + r["source_label"]] = dict(
                        session_phase="closing_auction",
                        phase_evidence=dict(
                            model="source-labelled-closing-stage.v1",
                            source_label=r["source_label"],
                            source_row_sha256=digest(IndexedQueries.project(bar, source)),
                            historical_phase_verified=False,
                            source_refs=["market"],
                        ),
                    )
            bundles.append(dict(source=source, bars=rows, unavailable=missing))
            reads.append(
                dict(
                    security=sid,
                    response_sha256=sha,
                    rows=len(rows),
                    unavailable_rows=len(missing),
                    binding=response["binding"],
                )
            )
            print(json.dumps(reads[-1]), flush=True)
    metadata["bar_annotations"] = annotations
    snapshot = IndexedQueries(release.receipt(), bundles)
    document = export_minute_research(snapshot, metadata)
    atomic(root / "input.json", document)
    report = {
        "release": release.receipt(),
        "securities": ids,
        "trading_dates": days,
        "minute_rows": len(document["minute"]),
        "unavailable_minutes": unavailable,
        "unavailable_rows": len(unavailable),
        "excluded_model_labels": excluded,
        "reads": reads,
        "input_sha256": digest(document),
        "limitations": metadata["limitations"],
    }
    atomic(root / "preparation.json", report)
    return report
