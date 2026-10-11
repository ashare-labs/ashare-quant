from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.service import PaperService

from .fixtures import close, config, decision, queue


def test_three_separate_days_continue_native_book(tmp_path):
    service = PaperService(tmp_path)
    session = config(str(tmp_path.name))
    sid = service.create(session)["id"]
    head = None
    reports = []
    for index, targets in enumerate(((100, 100), (0, 100), (100, 0))):
        day = queue(service, sid, session, index, targets, head)
        report = service.work(sid, day)
        reports.append(report)
        head = report["checkpoint_sha256"]
        assert report["native"]["cursor"] == (index + 1) * 4
        assert report["native"]["settlement_count"] == index + 1
        assert all(event["at"].startswith(day) for event in report["native"]["events"])
        assert report["native"]["reconciliation"]["status"] == "passed"
    assert Decimal(reports[0]["native"]["account"]["total_cash"]) == Decimal("6990")
    assert Decimal(reports[-1]["native"]["account"]["total_cash"]) == Decimal("8972")
    assert reports[1]["native"]["before"]["positions"]["000001.XSHE"]["sellable"] == 100
    ids = [t["id"] for r in reports for t in r["native"]["trades"]]
    assert len(ids) == len(set(ids)) == 5
    assert service.work(sid, day) == reports[-1]


def test_native_uses_open_to_trade_and_close_to_value(tmp_path):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    intent = service.prepare(sid, decision(session, 0, (100, 100)))
    execution = close(intent["day"], intent["intent_id"])
    for row, price in zip(execution["rows"], ("9.5", "19"), strict=True):
        row["open"] = row["low"] = price
    service.seal_close(sid, execution)
    native = service.work(sid, intent["day"])["native"]
    assert [Decimal(t["price"]) for t in native["trades"]] == [Decimal("9.5"), Decimal("19")]
    assert all(t["at"].endswith("09:30:00") for t in native["trades"])
    assert Decimal(native["account"]["total_cash"]) == Decimal("7140")
    assert Decimal(native["account"]["nav"]) == Decimal("10140")
    assert native["account"]["positions"]["000001.XSHE"]["sellable"] == 0
    assert native["liquidity_claim"] is False


def pending_order_case(tmp_path, fault):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    intent = service.prepare(sid, decision(session, 0, (100, 100)))
    execution = close(intent["day"], intent["intent_id"])
    for row, price in zip(execution["rows"], ("11", "22"), strict=True):
        row["open"] = row["high"] = price
    service.seal_close(sid, execution)
    with pytest.raises(DomainError, match="Native worker exited -9"):
        service.work(sid, intent["day"], fault=fault)
    service.control(sid, "resume", None, "recover pending native reservations")
    report = service.work(sid, intent["day"])
    native = report["native"]
    assert native["trades"] == []
    assert len(native["orders"]) == 2
    assert {o["status"] for o in native["orders"].values()} == {"CANCELLED"}
    opening = native["events"][1]["account"]
    assert len(opening["open_orders"]) == 2 and Decimal(opening["frozen_cash"]) > 0
    assert Decimal(native["account"]["frozen_cash"]) == 0
    assert Decimal(native["account"]["total_cash"]) == 10000
    assert native["target_gaps"] == {"000001.XSHE": 100, "600000.XSHG": 100}
    assert service.status(sid)["events"][-1]["details"]["code"] == "PAPER_UNFILLED_TARGETS"


def test_pending_native_order_restores_then_cancels_without_late_close_fill(tmp_path):
    pending_order_case(tmp_path, "event_committed:2")


def test_crash_after_native_cancel_before_event_commit_restores_reservation_once(tmp_path):
    pending_order_case(tmp_path, "inside_native_cancel")


def test_corrupt_committed_evidence_never_displays_success(tmp_path):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    day = queue(service, sid, session, 0, (100, 100))
    service.work(sid, day)
    path = tmp_path / sid / day / "native.chk"
    path.write_bytes(path.read_bytes()[:-3] + b"bad")
    assert service.status(sid)["days"][0]["status"] == "evidence_conflict"
    with pytest.raises(DomainError) as corrupt:
        service.report(sid, day)
    assert corrupt.value.code == "PAPER_CHECKPOINT_CHANGED"
    with pytest.raises(DomainError):
        service.prepare(
            sid, decision(session, 1, (100, 100), service.status(sid)["session"]["head"])
        )


def test_prior_mark_revision_is_blocked_but_old_intent_retry_is_idempotent(tmp_path):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    original = decision(session, 0, (100, 100))
    intent = service.prepare(sid, original)
    execution = close(intent["day"], intent["intent_id"])
    execution["rows"][0].update(close="10.5", high="10.5")
    service.seal_close(sid, execution)
    report = service.work(sid, intent["day"])
    assert service.prepare(sid, original)["intent_id"] == intent["intent_id"]
    with pytest.raises(DomainError) as revised:
        service.prepare(sid, decision(session, 1, (100, 100), report["checkpoint_sha256"]))
    assert revised.value.code == "PAPER_PRIOR_MARK_REVISION"
