"""ND-1: the selected continuation, including its window, is immutable."""

import copy
import json
from datetime import datetime

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest, sha
from ashare_quant.paper import intraday, online
from ashare_quant.paper import intraday_continuation as ext
from ashare_quant.paper.intraday_contracts import IntradayPlan
from ashare_quant.paper.online_contracts import OnlineDecision
from ashare_quant.paper.service import engine_pin

from .test_intraday_continuation import bind, future_packets, parent


@pytest.mark.parametrize(
    "damage", ["window", "bound_at", "missing_event", "digest", "parent", "day", "duplicate_event"]
)
def test_changed_binding_rejects_load_repeat_bind_and_prepare_before_any_successor(
    tmp_path, monkeypatch, damage
):
    service, reader, plan, _ = parent(tmp_path)
    plan = IntradayPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "start_at": "2026-10-15T09:35:00+08:00",
            "intent_deadline": "2026-10-15T09:40:00+08:00",
            "execution_deadline": "2026-10-15T09:50:00+08:00",
        }
    )
    bind(service, reader, plan, monkeypatch)
    state = json.loads(service.path.read_text())
    event = next(e for e in state["events"] if e["kind"] == "holding_successor_bound")
    original_identity = event["details"]["manifest"]
    if damage == "window":
        state["continuation"]["plan"].update(
            start_at="2026-10-15T10:00:00+08:00",
            intent_deadline="2026-10-15T10:05:00+08:00",
            execution_deadline="2026-10-15T10:10:00+08:00",
        )
    elif damage == "bound_at":
        state["continuation"]["bound_at"] = "2026-10-14T18:01:00+08:00"
    elif damage == "missing_event":
        state["events"].remove(event)
    elif damage == "duplicate_event":
        state["events"].append(copy.deepcopy(event))
    elif damage == "digest":
        event["details"]["manifest"] = "a" * 64
    elif damage == "parent":
        event["details"]["parent"] = "b" * 64
    else:
        event["day"] = "2026-10-16"
    if damage in {"window", "bound_at"}:
        assert digest(state["continuation"]) != original_identity
        assert event["details"]["manifest"] == original_identity
    service.path.write_text(json.dumps(state))
    before = service.path.read_bytes()
    altered = IntradayPlan.model_validate(state["continuation"]["plan"])
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_BINDING_CHANGED"):
        service._load()
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_BINDING_CHANGED"):
        ext.bind(
            service,
            reader,
            altered,
            sha((service.root / "continuation-origin.json").read_bytes()),
            digest(engine_pin()),
            digest(state["engine_pin"]),
        )
    monkeypatch.setattr(
        intraday, "now", lambda: datetime.fromisoformat("2026-10-15T10:00:31+08:00")
    )
    monkeypatch.setattr(online, "now", intraday.now)
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_BINDING_CHANGED"):
        intraday.prepare(service, reader, altered, "c" * 64)
    assert service.path.read_bytes() == before
    assert not (service.root / "2026-10-15").exists()


def test_new_plan_cannot_reuse_an_existing_binding_identity(tmp_path, monkeypatch):
    service, reader, plan, _ = parent(tmp_path)
    bind(service, reader, plan, monkeypatch)
    before = service.path.read_bytes()
    state = json.loads(before)
    altered = IntradayPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "start_at": "2026-10-15T10:15:00+08:00",
            "intent_deadline": "2026-10-15T10:20:00+08:00",
            "execution_deadline": "2026-10-15T10:30:00+08:00",
        }
    )
    with pytest.raises(DomainError, match="PAPER_CONTINUATION_IDEMPOTENCY_CONFLICT"):
        ext.bind(
            service,
            reader,
            altered,
            sha((service.root / "continuation-origin.json").read_bytes()),
            digest(engine_pin()),
            digest(state["engine_pin"]),
        )
    assert service.path.read_bytes() == before


def test_worker_checks_the_selected_binding_not_only_its_mutable_manifest(tmp_path, monkeypatch):
    service, reader, plan, _ = parent(tmp_path)
    bind(service, reader, plan, monkeypatch)
    ids = future_packets(reader)
    clock = {"at": datetime.fromisoformat("2026-10-15T10:00:31+08:00")}
    monkeypatch.setattr(intraday, "now", lambda: clock["at"])
    monkeypatch.setattr(online, "now", lambda: clock["at"])
    intraday.prepare(service, reader, plan, ids["admission"])
    clock["at"] = datetime.fromisoformat("2026-10-15T10:03:31+08:00")
    intraday.deliver(service, reader, plan, ids["execution"])
    service.advance(str(plan.day), "opening")
    request = json.loads((service.root / str(plan.day) / "opening-request.json").read_text())
    state, config = service._load()
    selected = next(e for e in state["events"] if e["kind"] == "holding_successor_bound")
    assert request["continuation_binding"] == selected
    ext.verify_request(
        request, config, OnlineDecision.model_validate(request["decision"]), engine_pin()
    )
    before = service.path.read_bytes()
    for damage in ("missing", "digest", "bound_at"):
        changed = copy.deepcopy(request)
        if damage == "missing":
            changed.pop("continuation_binding")
        elif damage == "digest":
            changed["continuation_binding"]["details"]["manifest"] = "d" * 64
        else:
            changed["continuation"]["bound_at"] = "2026-10-14T18:01:00+08:00"
            changed["decision"]["strategy_receipt"]["continuation"] = changed["continuation"]
        with pytest.raises(DomainError, match="PAPER_CONTINUATION_BINDING_CHANGED"):
            ext.verify_request(
                changed, config, OnlineDecision.model_validate(changed["decision"]), engine_pin()
            )
    assert service.path.read_bytes() == before
