"""Controlled clocks/transport only; never actual calendar acceptance."""

import copy
import json
from datetime import datetime
from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_adapter import execution_from_owner
from ashare_quant.paper.online_contracts import OnlineClose
from ashare_quant.paper.risk import close_gate
from ashare_quant.storage.artifacts import digest
from tests.paper.test_intraday import setup


def opened_account(tmp_path, monkeypatch, received):
    service, reader, plan, ids, clock = setup(
        tmp_path,
        monkeypatch,
        partial=True,
        volumes={"600000": 33400, "600036": 221700, "601318": 120400},
    )
    row = intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    opened = service.advance(row["day"], "opening")
    daily = dict(
        signal_day=row["day"],
        signal_ready=True,
        revisions=[],
        completed_at=received,
        securities={
            sid: dict(
                rows=[dict(open=p, high=p, low=p, close=p, volume=1000000)],
                source=dict(observed_at=received),
            )
            for sid, p in zip(plan.universe, ("10", "20", "30"), strict=True)
        },
    )
    capture = digest(daily)

    def read(self, config, kind, requested):
        assert kind == "daily" and requested == capture
        return copy.deepcopy(daily)

    monkeypatch.setattr(type(reader), "read", read)
    return service, reader, row, opened, daily, capture, clock


@pytest.mark.parametrize(
    "received,execute,fault,retry",
    [
        (
            "2026-10-14T19:07:00+08:00",
            "2026-10-14T19:08:00+08:00",
            None,
            "2026-10-14T20:00:00+08:00",
        ),
        (
            "2026-10-14T23:50:00+08:00",
            "2026-10-15T07:00:00+08:00",
            None,
            "2026-10-15T07:01:00+08:00",
        ),
        (
            "2026-10-14T17:45:00+08:00",
            "2026-10-14T17:46:00+08:00",
            "inside_native_settlement",
            "2026-10-15T07:00:00+08:00",
        ),
        (
            "2026-10-14T19:07:00+08:00",
            "2026-10-14T19:08:00+08:00",
            "event_committed:3",
            "2026-10-14T19:11:00+08:00",
        ),
        (
            "2026-10-14T19:07:00+08:00",
            "2026-10-14T19:08:00+08:00",
            "after_complete_checkpoint",
            "2026-10-15T07:00:00+08:00",
        ),
    ],
)
def test_late_native_settlement_retains_partial_fills_once(
    tmp_path, monkeypatch, received, execute, fault, retry
):
    service, reader, row, opened, _, capture, clock = opened_account(
        tmp_path, monkeypatch, received
    )
    day = row["day"]
    original = {
        name: (service.root / day / name).read_bytes()
        for name in ("opening.chk", "opening-report.json", "opening-request.json")
    }
    clock["at"] = datetime.fromisoformat(execute)
    service.deliver_observed(reader, day, "closing", capture)
    if fault:
        with pytest.raises(DomainError, match="exited -9"):
            service.advance(day, "closing", fault=fault)
        clock["at"] = datetime.fromisoformat(retry)
        service = OnlineService(service.root)
        service.resume("controlled delayed settlement restart")
    closed = service.advance(day, "closing")
    native = closed["native"]
    assert native["trades"] == opened["native"]["trades"]
    assert native["orders"] == opened["native"]["orders"]
    assert native["account"]["total_cash"] == opened["native"]["account"]["total_cash"]
    assert Decimal(native["account"]["total_cash"]) == 9975
    assert native["account"]["positions"]["600000.XSHG"]["quantity"] == 300
    assert native["account"]["open_orders"] == []
    assert native["cursor"] == 4 and native["settlement_count"] == 1
    expected_at = str(datetime.fromisoformat(received).replace(tzinfo=None))
    assert all(event["at"] == expected_at for event in native["events"][2:])
    state = json.loads(service.path.read_bytes())
    assert state["events"][-1]["at"] == clock["at"].isoformat()
    assert state["last_day"] == day and set(state["days"]) == {day}
    sealed = service.path.read_bytes()
    checkpoint = (service.root / day / "closing.chk").read_bytes()
    clock["at"] = datetime.fromisoformat(retry)
    service = OnlineService(service.root)
    service.deliver_observed(reader, day, "closing", capture)
    assert service.advance(day, "closing") == closed
    assert service.path.read_bytes() == sealed
    assert (service.root / day / "closing.chk").read_bytes() == checkpoint
    assert all((service.root / day / name).read_bytes() == raw for name, raw in original.items())
    (tmp_path / "late-close-proof.json").write_text(
        json.dumps(
            dict(
                evidence_kind="controlled_clock_transport_native_fixture_only",
                real_calendar_acceptance=False,
                received_at=received,
                first_attempt_at=execute,
                fault=fault,
                final_committed_at=state["events"][-1]["at"],
                opened=opened,
                closed=closed,
            ),
            indent=2,
        )
    )


def test_invalid_late_inputs_and_revisions_still_rejected(tmp_path, monkeypatch):
    service, reader, row, _, daily, capture, clock = opened_account(
        tmp_path, monkeypatch, "2026-10-14T19:07:00+08:00"
    )
    day = row["day"]
    before = service.path.read_bytes()
    clock["at"] = datetime.fromisoformat("2026-10-14T19:06:00+08:00")
    with pytest.raises(DomainError, match="PAPER_CLOSE_NOT_YET_RECEIVED"):
        service.deliver_observed(reader, day, "closing", capture)
    clock["at"] = datetime.fromisoformat("2026-10-14T19:08:00+08:00")
    daily["revisions"] = [dict(changes="controlled source revision")]
    with pytest.raises(DomainError, match="PAPER_OBSERVATION_REVISION"):
        service.deliver_observed(reader, day, "closing", capture)
    daily["revisions"] = []
    _, config = service._load()
    packet = execution_from_owner(config, row, reader, "closing", capture)
    from ashare_quant.paper.online_contracts import OnlineDecision

    decision = OnlineDecision.model_validate(row["decision"])
    for changes, error in [
        ({"complete": False}, "PAPER_CLOSE_DATA_INCOMPLETE"),
        ({"conflict": True}, "PAPER_CLOSE_DATA_INCOMPLETE"),
        ({"volume": 0}, "PAPER_CLOSE_DATA_INCOMPLETE"),
        ({"source_watermark": "2026-10-13"}, "PAPER_CLOSE_DATA_INCOMPLETE"),
        ({"observed_at": "2026-10-14T19:07:01+08:00"}, "PAPER_CLOSE_NOT_OBSERVED"),
        ({"observed_at": "2026-10-15T00:01:00+08:00"}, "PAPER_CLOSE_NOT_OBSERVED"),
    ]:
        value = packet.model_dump(mode="json")
        value["rows"][0].update(changes)
        with pytest.raises(DomainError, match=error):
            close_gate(config, decision, OnlineClose.model_validate(value))
    value = packet.model_dump(mode="json")
    value["received_at"] = "2026-10-15T00:01:00+08:00"
    with pytest.raises(DomainError, match="PAPER_CLOSE_DEADLINE"):
        close_gate(config, decision, OnlineClose.model_validate(value))
    assert service.path.read_bytes() == before


def test_legacy_close_deadline_retained_but_not_a_calendar_cutoff(tmp_path, monkeypatch):
    service, reader, row, _, _, capture, clock = opened_account(
        tmp_path, monkeypatch, "2026-10-14T17:45:00+08:00"
    )
    _, config = service._load()
    value = execution_from_owner(config, row, reader, "closing", capture).model_dump(mode="json")
    value["deadline"] = "2026-10-14T18:45:00+08:00"
    clock["at"] = datetime.fromisoformat("2026-10-14T17:46:00+08:00")
    service._deliver("closing", value)  # Explicit controlled legacy sealed-contract fixture.
    sealed = json.loads(service.path.read_bytes())["days"][row["day"]]["close"]
    clock["at"] = datetime.fromisoformat("2026-10-15T07:00:00+08:00")
    closed = OnlineService(service.root).advance(row["day"], "closing")
    assert closed["native"]["settlement_count"] == 1
    assert json.loads(service.path.read_bytes())["days"][row["day"]]["close"] == sealed


def test_historical_deadline_contract_is_unchanged():
    from ashare_quant.paper.contracts import CloseInput, Decision
    from tests.paper.fixtures import close, config, decision

    session = config()
    intent = Decision.model_validate(decision(session, 0, (100, 100)))
    packet = close(str(intent.trading_date), digest("historical-intent"))
    close_gate(session, intent, CloseInput.model_validate(packet))
    for field, at in (("received_at", "18:01"), ("deadline", "18:45")):
        changed = {**packet, field: f"{intent.trading_date}T{at}:00+08:00"}
        with pytest.raises(DomainError, match="PAPER_CLOSE_DEADLINE"):
            close_gate(session, intent, CloseInput.model_validate(changed))
