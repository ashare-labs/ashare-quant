"""Controlled sources and clocks; native partial fills, never live evidence."""

from datetime import datetime
from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday
from ashare_quant.paper.online import OnlineService

from .test_intraday import setup


@pytest.mark.parametrize(
    "crash,volume,filled",
    [
        (None, 67900, 600),
        ("inside_native_trade", 67900, 600),
        ("after_opening_checkpoint", 67900, 600),
        (None, 19999, 100),
    ],
)
def test_native_partial_fill_cancels_remainder_and_recovers(
    tmp_path, monkeypatch, crash, volume, filled
):
    service, reader, plan, ids, clock = setup(
        tmp_path,
        monkeypatch,
        partial=True,
        volumes={"600000": volume, "600036": 105700, "601318": 136400},
    )
    intent = intraday.prepare(service, reader, plan, ids["admission"])
    targets = intent["decision"]["targets"]
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    if crash:
        with pytest.raises(DomainError, match="exited -9"):
            service.advance(str(plan.day), "opening", fault=crash)
        service = OnlineService(service.root)
        service.resume("controlled crash during partial execution; retain sealed event")
    report = service.advance(str(plan.day), "opening")
    native = report["native"]
    trades = native["trades"]
    assert len(trades) == len({t["id"] for t in trades}) == 3
    expected = {**targets, "600000.XSHG": filled}
    assert {t["security"]: t["quantity"] for t in trades} == expected
    assert sum(Decimal(t["cost"]) for t in trades) == 15
    account = native["account"]
    assert Decimal(account["total_cash"]) == Decimal(30000) - sum(
        Decimal(t["price"]) * t["quantity"] + Decimal(t["cost"]) for t in trades
    )
    assert Decimal(account["frozen_cash"]) == 0 and account["open_orders"] == []
    assert all(p["sellable"] == 0 for p in account["positions"].values())
    orders = list(native["orders"].values())
    partial = next(o for o in orders if o["security"] == "600000.XSHG")
    assert partial["quantity"] == targets["600000.XSHG"]
    assert partial["filled"] == filled and partial["status"] == "CANCELLED"
    assert partial["remaining"] == targets["600000.XSHG"] - filled
    assert native["capacity_policy"]["remainder"] == "cancel_after_minute"
    assert native["capacity_policy"]["exchange_rule"] is False
    state = service.path.read_bytes()
    checkpoint = (service.root / str(plan.day) / "opening.chk").read_bytes()
    service = OnlineService(service.root)
    intraday.deliver(service, reader, plan, ids["execution"])
    assert service.advance(str(plan.day), "opening") == report
    assert service.path.read_bytes() == state
    assert (service.root / str(plan.day) / "opening.chk").read_bytes() == checkpoint


def test_no_complete_lot_cancels_native_orders_without_fake_fill(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(
        tmp_path,
        monkeypatch,
        partial=True,
        volumes={"600000": 9999, "600036": 9999, "601318": 9999},
    )
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    report = service.advance(str(plan.day), "opening")["native"]
    assert report["trades"] == []
    assert len(report["orders"]) == 3
    assert all(o["status"] == "CANCELLED" for o in report["orders"].values())
    assert Decimal(report["account"]["total_cash"]) == 30000
    assert Decimal(report["account"]["frozen_cash"]) == 0
    assert report["account"]["open_orders"] == []


def test_partial_model_still_rejects_future_minute_before_any_order(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch, partial=True)
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    original = reader.intraday

    def future(config, capture):
        packet = original(config, capture)
        for row in packet["assessment"]["rows"]:
            row["execution"]["minute_end"] = "2026-10-14T10:04:00+08:00"
        return packet

    monkeypatch.setattr(type(reader), "intraday", lambda self, *args: future(*args))
    before = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_INTRADAY_MINUTE_CAUSALITY"):
        intraday.deliver(service, reader, plan, ids["execution"])
    assert service.path.read_bytes() == before and not list(service.root.rglob("*.chk"))


def test_partial_capacity_does_not_override_native_risk(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch, partial=True)
    from ashare_quant.paper.online_contracts import OnlineSession

    config = intraday.config_for(plan, "partial-risk-fixture").model_dump(mode="json")
    config["risk"]["max_order_value"] = "1"
    service = OnlineService(tmp_path / "risk")
    service.create(OnlineSession.model_validate(config))
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    with pytest.raises(DomainError, match="Native worker exited"):
        service.advance(str(plan.day), "opening")
    import json

    error = json.loads((service.root / str(plan.day) / "error.json").read_text())
    assert error["code"] == "PAPER_ORDER_VALUE_LIMIT"
    assert service.status()["latest_account"] is None
