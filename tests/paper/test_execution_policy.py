import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.paper.api import create_app
from ashare_quant.paper.contracts import SessionConfig
from ashare_quant.paper.risk import profiles
from ashare_quant.paper.service import PaperService

from .fixtures import close, config, decision, queue
from .test_replay import bound


def policy_config(account):
    value = config(account).model_dump(mode="json")
    value["risk"].update(
        profile="target_then_native_cash_v1",
        max_gross="0.98",
        max_single="0.98",
        execution_max_single="1",
        max_order_nav_ratio="1",
        max_turnover="2",
        cash_buffer="0",
        max_order_value="100000",
    )
    return SessionConfig.model_validate(value)


def test_policy_profile_and_public_fork_do_not_mutate_old_account(tmp_path):
    example = Path(__file__).resolve().parents[2] / "examples/daily-paper-research-risk.json"
    assert json.loads(example.read_text()) == profiles()["target_then_native_cash_v1"]
    service, sid = bound(tmp_path, risk={})
    old = service.status(sid)
    with TestClient(create_app(service.root)) as api:
        risk = api.get("/api/v1/paper/risk-profiles").json()["target_then_native_cash_v1"]
        response = api.post(
            f"/api/v1/paper/sessions/{sid}/fork", json={"account_id": "review-new", "risk": risk}
        )
        assert response.status_code == 201
        target = response.json()["session_id"]
        assert target != sid
        assert (
            api.post(
                f"/api/v1/paper/sessions/{sid}/fork",
                json={"account_id": "review-new", "risk": risk},
            ).json()["session_id"]
            == target
        )
        result = api.post(
            f"/api/v1/paper/sessions/{target}/advance",
            json={"day": "2020-02-03", "expected_parent": None, "reason": "explicit simulation"},
        ).json()
        assert len(result["native"]["trades"]) == 8
        assert result["native"]["refusals"] == []
    assert service.status(sid) == old


def test_native_sells_release_cash_before_buys_and_duplicate_keeps_book(tmp_path):
    service = PaperService(tmp_path)
    cfg = policy_config(tmp_path.name)
    sid = service.create(cfg)["id"]
    first = service.work(sid, queue(service, sid, cfg, 0, (900, 0)))
    day = queue(service, sid, cfg, 1, (0, 400), first["checkpoint_sha256"])
    second = service.work(sid, day)
    assert [t["side"] for t in second["native"]["trades"]] == ["SELL", "BUY"]
    submissions = second["native"]["submissions"]
    assert Decimal(submissions[1]["cash_before"]) > Decimal(submissions[0]["cash_before"])
    assert Decimal(second["native"]["account"]["total_cash"]) == 1976
    assert service.work(sid, day) == second


def gap_case(root, fault=None):
    service = PaperService(root)
    cfg = policy_config(root.name)
    sid = service.create(cfg)["id"]
    d = decision(cfg, 0, (600, 200))
    for row, price, upper, lower in zip(
        d["rows"], ("9.8", "19.6"), ("10.78", "21.56"), ("8.82", "17.64"), strict=True
    ):
        row.update(previous_close=price, upper_limit=upper, lower_limit=lower)
    intent = service.prepare(sid, d)
    day = intent["day"]
    c = close(day, intent["intent_id"])
    for row, price, opened in zip(c["rows"], ("9.8", "19.6"), ("10.68", "21.36"), strict=True):
        row.update(open=opened, high=opened, close=price, low=price)
    service.seal_close(sid, c)
    if fault:
        with pytest.raises(DomainError):
            service.work(sid, day, fault=fault)
        assert service.status(sid)["session"]["head"] is None
        service.control(sid, "resume", None, "recover same frozen gap day")
    report = service.work(sid, day)
    assert service.work(sid, day) == report
    return report["native"]


def test_gap_rejects_unaffordable_buy_without_rolling_back_fills_or_double_execution(tmp_path):
    uninterrupted = gap_case(tmp_path / "baseline")
    recovered = gap_case(tmp_path / "recovered", "inside_native_trade")
    for native in (uninterrupted, recovered):
        assert len(native["trades"]) == 1
        assert Decimal(native["account"]["total_cash"]) == 3587
        assert native["refusals"][0]["code"] == "PAPER_CASH_BUFFER"
        assert native["refusals"][0]["measured"] == "4277.00"
        assert Decimal(native["refusals"][0]["cap"]) == Decimal("3587.00")
        assert "4277" in native["refusals"][0]["message"]
        assert native["submissions"][1]["cash_before"] == native["submissions"][1]["cash_after"]
        assert native["settlement_count"] == 1
    for key in ("account", "orders", "trades", "events", "refusals", "submissions"):
        assert uninterrupted[key] == recovered[key]
