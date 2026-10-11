import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.paper.api import create_app
from ashare_quant.paper.execution import bind, ingest
from ashare_quant.paper.replay import advance
from ashare_quant.paper.risk import profiles
from ashare_quant.paper.service import PaperService


def bound(tmp_path, window="w2020", assumptions=None):
    configured = os.environ.get("ASHARE_PAPER_AD_PIN")
    if not configured:
        pytest.skip("Explicit sealed AD v12 source required; no fabricated replacement")
    source = Path(configured)
    c = json.loads((source / "consumer.json").read_text())
    service = PaperService(tmp_path)
    r = bind(
        service,
        Path(__file__).resolve().parents[2] / "examples/daily_portfolio/manifest.json",
        source / "research/binding.json",
        c["research_sha"],
        source / "execution/binding.json",
        c["execution_sha"],
        source / c["wheel"],
        c["wheel_sha"],
        c["ad_commit"],
        "equal-weight",
        window,
        tmp_path.name,
        accepted_assumptions=c["accepted_assumptions"] if assumptions is None else assumptions,
        accept_listing_continuity=True,
        cash_credit_policy=c["cash_credit_policy"],
        risk=profiles()["target_then_native_cash_v1"],
    )
    return service, r["session_id"]


def test_real_ad_day_append_restart_and_native_recovery_are_idempotent(tmp_path):
    service, sid = bound(tmp_path)
    with TestClient(create_app(service.root)) as api:
        missing = api.post(
            f"/api/v1/paper/sessions/{sid}/advance",
            json={
                "day": "2020-02-03",
                "expected_parent": None,
                "reason": "missing delivery must not execute",
            },
        )
        assert (
            missing.status_code == 409
            and missing.json()["error"]["code"] == "PAPER_EXECUTION_DAY_MISSING"
        )
    with pytest.raises(DomainError, match="Packet staged"):
        ingest(service, sid, "2020-02-03", None, fault="after_packet_write")
    assert service.view(sid)["replay"]["delivery"]["received"] == []
    packet = ingest(PaperService(service.root), sid, "2020-02-03", None)
    assert ingest(service, sid, "2020-02-03", None)["packet_sha256"] == packet["packet_sha256"]
    with pytest.raises(DomainError, match="PAPER_PRIOR_DAY_NOT_COMMITTED"):
        ingest(service, sid, "2020-02-04", None)
    with pytest.raises(DomainError):
        advance(service, sid, "2020-02-03", None, fault="inside_native_trade")
    assert service.status(sid)["session"]["head"] is None
    service.control(sid, "resume", None, "recover same real AD packet")
    report = advance(service, sid, "2020-02-03", None)
    assert len(report["native"]["trades"]) == 8
    assert advance(service, sid, "2020-02-03", None) == report
    assert report["native"]["account"]["total_cash"] == "142628.8658"
    parent = report["checkpoint_sha256"]
    with pytest.raises(DomainError, match="Packet admitted"):
        ingest(service, sid, "2020-02-04", parent, fault="after_packet_commit")
    repeat = ingest(PaperService(service.root), sid, "2020-02-04", parent)
    assert repeat["already_received"]
    second = advance(service, sid, "2020-02-04", parent)
    assert second["native"]["cursor"] == 8 and second["native"]["settlement_count"] == 2
    assert not second["native"]["trades"]
    receipts = [e for e in service.view(sid)["events"] if e["kind"] == "ad_day_received"]
    assert len(receipts) == 2


def test_modified_ad_packet_is_not_consumed(tmp_path):
    service, sid = bound(tmp_path)
    ingest(service, sid, "2020-02-03", None)
    path = service.root / sid / "deliveries/2020-02-03.json"
    packet = json.loads(path.read_text())
    packet["data"]["rows"]["000001.XSHE"]["is_st"] = True
    path.write_text(json.dumps(packet))
    with pytest.raises(DomainError, match="PAPER_DELIVERY_CHANGED"):
        advance(service, sid, "2020-02-03", None)
    assert service.status(sid)["session"]["head"] is None


def test_fixed_ad_2024_unknowns_remain_blocked_without_fallback(tmp_path):
    service, sid = bound(tmp_path, "w2024")
    gaps = service.view(sid)["replay"]["preflight"]["gaps"]
    missing = [x for x in gaps if x["field"] in {"is_st", "upper_limit", "lower_limit"}]
    assert len(missing) == 696
    assert {x["security"] for x in missing} == {
        "600000.XSHG",
        "600036.XSHG",
        "600519.XSHG",
        "601318.XSHG",
    }
    with pytest.raises(DomainError) as caught:
        ingest(service, sid, "2024-01-02", None)
    assert caught.value.code == "PAPER_AD_NOT_ADMITTED"
    assert service.view(sid)["head"] is None
    assert not service.view(sid)["replay"]["delivery"]["received"]


def test_ad_explicit_model_choices_are_required(tmp_path):
    service, sid = bound(tmp_path, assumptions=[])
    assert not service.view(sid)["replay"]["preflight"]["ready"]
    with pytest.raises(DomainError) as caught:
        ingest(service, sid, "2020-02-03", None)
    assert caught.value.code == "PAPER_AD_NOT_ADMITTED"
