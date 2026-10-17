"""Isolated real owner reader/native runtime with synthetic clocks and quotes."""

import copy
import json
from datetime import datetime

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper import intraday, online
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online import OnlineService
from ashare_quant.storage.artifacts import digest
from tests.paper.synthetic_owner import prepare_owner


def setup(tmp_path, monkeypatch, *, partial=False, volumes=None):
    python, source, ids = prepare_owner(tmp_path, monkeypatch, volumes)
    daily = dict(
        signal_day="2026-10-13",
        signal_ready=True,
        revisions=[],
        completed_at="2026-10-13T17:45:00+08:00",
        calendar=dict(
            next_open="2026-10-14",
            open_days=["2026-10-13", "2026-10-14", "2026-10-15"],
            source=dict(observed_at="2026-10-13T17:45:00+08:00"),
        ),
        securities={
            s: dict(
                rows=[dict(close=p)] * 120, source=dict(observed_at="2026-10-13T17:45:00+08:00")
            )
            for s, p in zip(
                intraday.UNIVERSE
                if hasattr(intraday, "UNIVERSE")
                else ("600000.XSHG", "600036.XSHG", "601318.XSHG"),
                ("10", "20", "30"),
                strict=True,
            )
        },
    )

    class Reader(intraday.IntradayReader):
        def read(self, config, kind, capture):
            assert kind == "daily" and capture == digest(daily)
            return copy.deepcopy(daily)

    reader = Reader(python, source, tmp_path / "ad", tmp_path / "phases")
    owned = reader.reviewed_plan()
    extra = (
        dict(
            schema_version="paper-intraday-plan.v2",
            model="prior-close-intent-minute-partial-cancel.v2",
            remainder_policy="cancel_after_minute",
        )
        if partial
        else {}
    )
    plan = IntradayPlan(
        **extra,
        day="2026-10-14",
        start_at="2026-10-14T10:00:00+08:00",
        intent_deadline="2026-10-14T10:05:00+08:00",
        execution_deadline="2026-10-14T10:10:00+08:00",
        admission_reader_sha256=reader.pin(),
        action_baseline_sha256=owned["action_baseline_sha256"],
        daily_capture=digest(daily),
    )
    service = OnlineService(tmp_path / "native")
    service.create(intraday.config_for(plan, "intraday-fixture-only"))
    clock = {"at": datetime.fromisoformat("2026-10-14T10:00:31+08:00")}
    monkeypatch.setattr(intraday, "now", lambda: clock["at"])
    monkeypatch.setattr(online, "now", lambda: clock["at"])
    return service, reader, plan, ids, clock


def test_native_future_minute_fees_holdings_crash_and_duplicate(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    row = intraday.prepare(service, reader, plan, ids["admission"])
    assert intraday.prepare(service, reader, plan, ids["admission"]) == row
    assert service.status()["latest_account"] is None and not list(service.root.rglob("*.chk"))
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    with pytest.raises(DomainError, match="exited -9"):
        service.advance(str(plan.day), "opening", fault="inside_native_trade")
    service = OnlineService(service.root)
    service.resume("controlled intraday crash, same sealed quote")
    report = service.advance(str(plan.day), "opening")
    assert len(report["native"]["trades"]) == 3
    assert all(
        t["at"].endswith("10:03:30") and float(t["cost"]) > 0 for t in report["native"]["trades"]
    )
    assert all(t["price"] in {"10.01", "20.01", "30.01"} for t in report["native"]["trades"])
    assert all(p["sellable"] == 0 for p in report["native"]["account"]["positions"].values())
    assert report["native"]["settlement_count"] == 0
    first = service.path.read_bytes()
    native = (service.root / str(plan.day) / "opening.chk").read_bytes()
    clock["at"] = datetime.fromisoformat("2026-10-14T12:00:00+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    assert service.advance(str(plan.day), "opening") == report
    assert (
        service.path.read_bytes() == first
        and (service.root / str(plan.day) / "opening.chk").read_bytes() == native
    )
    (tmp_path / "fixture-summary.json").write_text(
        json.dumps(
            dict(
                evidence_kind="controlled_fixture_only",
                real_calendar_acceptance=False,
                report=report,
            ),
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "at", ["2026-10-14T09:59:59+08:00", "2026-10-14T10:05:01+08:00", "2026-10-14T12:00:00+08:00"]
)
def test_real_clock_gate_rejects_early_late_and_lunch(tmp_path, monkeypatch, at):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    before = service.path.read_bytes()
    clock["at"] = datetime.fromisoformat(at)
    with pytest.raises(DomainError):
        intraday.prepare(service, reader, plan, ids["admission"])
    assert service.path.read_bytes() == before and not list(service.root.rglob("*.chk"))


def test_minute_volume_limit_before_any_order(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    original = reader.intraday

    def low(config, capture):
        p = original(config, capture)
        for r in p["assessment"]["rows"]:
            r["execution"]["volume"] = 1
        return p

    monkeypatch.setattr(type(reader), "intraday", lambda self, *args: low(*args))
    before = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_INTRADAY_VOLUME_LIMIT"):
        intraday.deliver(service, reader, plan, ids["execution"])
    assert service.path.read_bytes() == before and not list(service.root.rglob("*.chk"))


def test_native_pretrade_risk_refuses_before_any_order(tmp_path, monkeypatch):
    service, reader, plan, ids, clock = setup(tmp_path, monkeypatch)
    config = intraday.config_for(plan, "risk-fixture-only")
    value = config.model_dump(mode="json")
    value["risk"]["max_order_value"] = "1"
    from ashare_quant.paper.online_contracts import OnlineSession

    service = OnlineService(tmp_path / "native-risk")
    service.create(OnlineSession.model_validate(value))
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-14T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    with pytest.raises(DomainError, match="Native worker exited"):
        service.advance(str(plan.day), "opening")
    assert service.status()["head"] is None and service.status()["latest_account"] is None
    error = json.loads((service.root / str(plan.day) / "error.json").read_bytes())
    assert error["code"] == "PAPER_ORDER_VALUE_LIMIT"
