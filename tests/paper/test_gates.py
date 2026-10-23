from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.paper.contracts import SessionConfig
from ashare_quant.paper.service import PaperService
from tests.paper.fixtures import DATES, close, config, decision, queue
from tests.support.paper_api import create_app


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda d: d["rows"][0].update(complete=False), "PAPER_DATA_STALE_OR_MISSING"),
        (
            lambda d: d["rows"][0].update(selected_watermark="2023-12-29"),
            "PAPER_DATA_STALE_OR_MISSING",
        ),
        (lambda d: d["rows"][0].update(suspended=None), "PAPER_TRADING_STATE_BLOCKED"),
        (lambda d: d["rows"][0].update(suspended=True), "PAPER_TRADING_STATE_BLOCKED"),
        (lambda d: d["rows"][0].update(is_st=True), "PAPER_TRADING_STATE_BLOCKED"),
        (lambda d: d["rows"][0].update(upper_limit=None), "PAPER_TRADING_RULES_UNKNOWN"),
        (
            lambda d: d["rows"][0].update(actions_known_absent=False),
            "PAPER_ACTION_COVERAGE_UNSUPPORTED",
        ),
        (lambda d: d["rows"].pop(), "PAPER_SECURITY_SET"),
        (lambda d: d.update(received_at=DATES[1] + "T09:06:00+08:00"), "PAPER_MISSED_DECISION"),
        (
            lambda d: d["rows"][0].update(observed_at=DATES[1] + "T09:01:00+08:00"),
            "PAPER_DATA_NOT_VISIBLE",
        ),
    ],
)
def test_bad_decision_is_visible_and_never_queued(tmp_path, mutation, code):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    request = decision(session, 0, (100, 100))
    mutation(request)
    with pytest.raises(DomainError) as rejected:
        service.prepare(sid, request)
    assert rejected.value.code == code
    status = service.status(sid)
    assert not status["days"] and status["session"]["head"] is None
    assert status["events"][-1]["details"]["code"] == code


def test_no_data_and_revised_intent_cannot_run(tmp_path):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    sid = service.create(session)["id"]
    request = decision(session, 0, (100, 100))
    prepared = service.prepare(sid, request)
    assert service.prepare(sid, request)["intent_id"] == prepared["intent_id"]
    with pytest.raises(DomainError) as pending:
        service.work(sid, prepared["day"])
    assert pending.value.code == "PAPER_CLOSE_PENDING"
    changed = deepcopy(request)
    changed["targets"]["000001.XSHE"] = 200
    with pytest.raises(DomainError) as conflict:
        service.prepare(sid, changed)
    assert conflict.value.code == "PAPER_IDEMPOTENCY_CONFLICT"
    bad = close(prepared["day"], prepared["intent_id"])
    bad["rows"][0]["conflict"] = True
    with pytest.raises(DomainError) as missing:
        service.seal_close(sid, bad)
    assert missing.value.code == "PAPER_CLOSE_DATA_INCOMPLETE"
    zero = close(prepared["day"], prepared["intent_id"], volume=0)
    with pytest.raises(DomainError) as empty:
        service.seal_close(sid, zero)
    assert empty.value.code == "PAPER_CLOSE_DATA_INCOMPLETE"
    assert not (tmp_path / sid / prepared["day"] / "native.chk").exists()


@pytest.mark.parametrize(
    "limits,targets,code",
    [
        ({}, (500, 0), "PAPER_CONCENTRATION_LIMIT"),
        ({"max_gross": "0.2"}, (100, 100), "PAPER_GROSS_LIMIT"),
        ({"max_order_value": "1000"}, (100, 0), "PAPER_ORDER_VALUE_LIMIT"),
        ({"max_holdings": 1}, (100, 100), "PAPER_HOLDINGS_LIMIT"),
        ({"max_turnover": "0.1"}, (100, 0), "PAPER_TURNOVER_LIMIT"),
        ({"cash_buffer": "0.99"}, (100, 0), "PAPER_CASH_BUFFER"),
        ({}, (101, 0), "PAPER_LOT_LIMIT"),
    ],
)
def test_risk_failure_stops_native_before_order(tmp_path, limits, targets, code):
    service, session = PaperService(tmp_path), config(tmp_path.name)
    settings = session.model_dump(mode="json")
    settings["risk"].update(limits)
    session = SessionConfig.model_validate(settings)
    sid = service.create(session)["id"]
    day = queue(service, sid, session, 0, targets)
    with pytest.raises(DomainError) as rejected:
        service.work(sid, day)
    assert rejected.value.code == code
    status = service.status(sid)
    assert status["session"]["head"] is None
    assert status["days"][-1]["recovery_required"]
    assert not (tmp_path / sid / day / "report.json").exists()


def test_local_api_boundaries_pause_and_failed_report(tmp_path):
    with TestClient(create_app(tmp_path)) as api:
        caps = api.get("/api/v1/paper/capabilities").json()
        assert caps["observed_data_enabled"] is False
        assert caps["continuous_paper_accepted"] is False
        session = config(tmp_path.name)
        created = api.post("/api/v1/paper/sessions", json=session.model_dump(mode="json"))
        assert created.status_code == 201
        sid = created.json()["session_id"]
        action = {"reason": "controlled pause", "expected_parent": None}
        assert api.post(f"/api/v1/paper/sessions/{sid}/pause", json=action).status_code == 200
        response = api.post(
            f"/api/v1/paper/sessions/{sid}/decisions", json=decision(session, 0, (0, 0))
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "PAPER_PAUSED"
        assert api.post(f"/api/v1/paper/sessions/{sid}/resume", json=action).status_code == 200
        assert response.headers["cache-control"] == "no-store"
        cfg = session.model_dump(mode="json")
        cfg["data_mode"] = "observed"
        assert api.post("/api/v1/paper/sessions", json=cfg).status_code == 422
