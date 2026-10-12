"""Falsification of primary-field checks using explicitly synthetic test doubles."""

from copy import deepcopy

import pytest

from ashare_quant.daily_portfolio.reference_audit import primary_audit

DAY = "2020-02-03"
OPEN = DAY + " 09:30:00"
CLOSE = DAY + " 15:30:00"


def fixture():
    ids = ["600000.XSHG", "600519.XSHG"]
    native, reference, fills = [], [], []
    for i, sid in enumerate(ids, 1):
        q, status = (200, "ACTIVE") if i == 1 else (100, "FILLED")
        native.append(
            dict(
                id=str(i), date=DAY, security=sid, side="BUY", quantity=q, filled=100, status=status
            )
        )
        reference.append(
            dict(
                order_id="J" + str(i),
                security=sid,
                is_buy=True,
                amount=q,
                filled=100,
                status="open" if i == 1 else "held",
            )
        )
        fills.append(
            dict(
                trade_id="T" + str(i),
                order_id="J" + str(i),
                security=sid,
                amount=100,
                price=i * 10,
                time=OPEN,
            )
        )
    end_native, end_reference = deepcopy(native), deepcopy(reference)
    end_native[0]["status"], end_reference[0]["status"] = "CANCELLED", "canceled"
    before, after = dict(cash="10000", equity="10000"), dict(cash="6990", equity="9990")
    events = [
        dict(
            kind="identity",
            time=DAY + " 08:00:00",
            volume_ratio="0.0001",
            evidence_kind="SYNTHETIC_TEST_DOUBLE_NOT_PLATFORM",
        )
    ]
    observations = []

    def snapshot(label, at, ns, js, cash, opened):
        events.append(
            dict(
                kind="snapshot",
                label=label,
                time=at,
                orders=deepcopy(js),
                trades=[] if label == "before_orders" else deepcopy(fills),
                open_orders=["J1"] if opened else [],
                account=dict(
                    available_cash=cash["cash"],
                    total_value=cash["equity"],
                    locked_cash=1000 if opened else 0,
                ),
            )
        )
        observations.append(
            dict(
                label=label,
                at=at,
                orders=deepcopy(ns),
                open_order_ids=["1"] if opened else [],
                account=cash,
                frozen_cash=1000 if opened else 0,
            )
        )

    snapshot("before_orders", OPEN, [], [], before, False)
    subs = []
    cash = 10000
    for i, (n, j) in enumerate(zip(native, reference), 1):
        events.append(
            dict(
                kind="submit",
                time=OPEN,
                security=n["security"],
                quantity=n["quantity"],
                cash_before=cash,
            )
        )
        prior, cash = cash, cash - i * 1000 - 5
        events.append(
            dict(
                kind="order_return",
                time=OPEN,
                security=n["security"],
                returned_none=False,
                order=deepcopy(j),
                cash_after=cash,
            )
        )
        subs.append(
            dict(
                date=DAY,
                at=OPEN,
                security=n["security"],
                quantity=n["quantity"],
                order_id=n["id"],
                returned_none=False,
                returned_order=deepcopy(n),
                cash_before=prior,
                cash_after=cash,
            )
        )
    snapshot("after_orders", OPEN, native, reference, after, True)
    daily = []
    for i, sid in enumerate(ids, 1):
        row = dict(
            date=DAY,
            security=sid,
            open=i * 10,
            high=i * 10,
            low=i * 10,
            close=i * 10,
            volume=2000000,
            money=2000000 * i * 10,
            suspended=False,
            upper_limit=i * 11,
            lower_limit=i * 9,
        )
        daily.append(row)
        record = {k: row[k] for k in ("open", "high", "low", "close", "volume", "money")}
        record.update(index=DAY, paused=False, high_limit=i * 11, low_limit=i * 9)
        events.append(dict(kind="daily_raw", time=CLOSE, security=sid, records=[record]))
    snapshot("day_close", CLOSE, end_native, end_reference, after, False)
    aq = dict(
        accounts=[dict(date=DAY)],
        decisions=[dict(date=DAY, rebalance=True)],
        submissions=subs,
        orders=end_native,
        observations=observations,
        trades=[
            dict(
                id=t["trade_id"],
                order_id=str(i),
                security=t["security"],
                side="BUY",
                quantity=100,
                price=t["price"],
                at=OPEN,
            )
            for i, t in enumerate(fills, 1)
        ],
        order_events=[dict(at=CLOSE, phase="CLOSE", order=end_native[0])],
    )
    spec = dict(
        securities=ids,
        expansion_case={"id": "SYNTHETIC_TEST_DOUBLE"},
        execution=dict(volume_ratio="0.0001", unfilled="cancel at session end"),
    )
    return events, aq, dict(daily=daily), spec


def test_complete_synthetic_observations_and_partial_lifecycle():
    report = primary_audit(*fixture())
    assert report["eligible_for_alignment"], report
    assert report["counts"] == {"raw_rows": 2, "orders": 2, "trades": 2}
    assert report["order_id_mapping"] == {"J1": "1", "J2": "2"}


@pytest.mark.parametrize(
    "field",
    ["open", "high", "low", "close", "volume", "money", "paused", "high_limit", "low_limit"],
)
def test_corrupted_raw_field_is_reported(field):
    args = fixture()
    raw = next(e for e in args[0] if e["kind"] == "daily_raw")["records"][0]
    raw[field] = True if field == "paused" else -1
    report = primary_audit(*args)
    assert not report["eligible_for_alignment"]
    assert any(d["code"] == "RAW_FIELD" for d in report["differences"])


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("amount", 300, "ORDER_QUANTITY"),
        ("filled", 0, "ORDER_FILLED"),
        ("status", "rejected", "ORDER_STATUS"),
        ("status", "UNKNOWN_TEST_DOUBLE", "UNKNOWN_ORDER_STATUS"),
    ],
)
def test_corrupted_order_quantity_fill_or_terminal_is_reported(field, value, code):
    args = fixture()
    for e in args[0]:
        for o in e.get("orders", []) + ([e["order"]] if e.get("order") else []):
            if o["order_id"] == "J1":
                o[field] = value
    report = primary_audit(*args)
    assert not report["eligible_for_alignment"]
    assert any(d["code"] == code for d in report["differences"])


@pytest.mark.parametrize(
    "missing",
    [
        "all_raw",
        "raw_volume",
        "order_return",
        "order_status",
        "open_orders",
        "aq_return",
        "aq_observation",
        "trade_time",
    ],
)
def test_missing_primary_fields_are_evidence_gaps(missing):
    events, aq, owner, spec = fixture()
    if missing == "all_raw":
        events = [e for e in events if e["kind"] != "daily_raw"]
    elif missing == "raw_volume":
        next(e for e in events if e["kind"] == "daily_raw")["records"][0].pop("volume")
    elif missing == "order_return":
        events.remove(next(e for e in events if e["kind"] == "order_return"))
    elif missing == "order_status":
        next(e for e in events if e["kind"] == "order_return")["order"].pop("status")
    elif missing == "open_orders":
        next(e for e in events if e.get("label") == "after_orders").pop("open_orders")
    elif missing == "aq_return":
        aq["submissions"][0].pop("returned_order")
    elif missing == "aq_observation":
        aq.pop("observations")
    else:
        for e in events:
            for t in e.get("trades", []):
                t.pop("time")
    report = primary_audit(events, aq, owner, spec)
    assert report["evidence_gaps"] and not report["evidence_complete"]
    assert not report["eligible_for_alignment"]


@pytest.mark.parametrize(
    "change", ["trade_order", "trade_time", "open_order", "submit_quantity", "raw_duplicate"]
)
def test_sequence_time_open_orders_and_requests_are_compared(change):
    args = fixture()
    events = args[0]
    if change == "trade_order":
        next(e for e in events if e.get("label") == "after_orders")["trades"].reverse()
    elif change == "trade_time":
        for e in events:
            for t in e.get("trades", []):
                t["time"] = DAY + " 09:31:00"
    elif change == "open_order":
        next(e for e in events if e.get("label") == "after_orders")["open_orders"] = [
            "MISSING_ORDER"
        ]
    elif change == "submit_quantity":
        next(e for e in events if e["kind"] == "submit")["quantity"] += 100
    else:
        raw = next(e for e in events if e["kind"] == "daily_raw")
        events.insert(events.index(raw) + 1, deepcopy(raw))
    report = primary_audit(*args)
    assert report["differences"] and not report["eligible_for_alignment"]


def test_immediate_cancellation_cannot_pass_frozen_session_end_policy():
    events, aq, owner, spec = fixture()
    aq["order_events"][0].update(at=OPEN, phase="EXECUTE")
    report = primary_audit(events, aq, owner, spec)
    assert report["contract_differences"][0]["code"] == "CANCELLED_BEFORE_SESSION_END"
    assert not report["eligible_for_alignment"]
