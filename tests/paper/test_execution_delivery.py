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


def bound(tmp_path, window="w2020", assumptions=None, strategy="equal-weight"):
    configured = os.environ.get("ASHARE_PAPER_AD_PIN")
    if not configured:
        pytest.skip("Explicit sealed AD source required; no fabricated replacement")
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
        strategy,
        window,
        tmp_path.name,
        accepted_assumptions=c["accepted_assumptions"] if assumptions is None else assumptions,
        accept_listing_continuity=True,
        cash_credit_policy=c["cash_credit_policy"],
        risk=profiles()["target_then_native_cash_v1"],
        consumer_contract=source / c["consumer_contract"] if c.get("consumer_contract") else None,
        consumer_contract_sha=c.get("consumer_contract_sha"),
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
    _, config = service.session(sid)
    if config.execution_admission_scope == "per_order":
        from copy import deepcopy

        from ashare_quant.paper.contracts import Decision
        from ashare_quant.paper.risk import order_permission

        d = Decision.model_validate_json(service.day(sid, "2020-02-03")["decision"])
        permit = d.order_admissions[0]
        security, side = permit["row"]["security"], permit["report"]["admitted_side"]
        order_permission(config, d, security, side)
        with pytest.raises(DomainError, match="PAPER_AD_ORDER_SCOPE"):
            order_permission(config, d, security, "SELL" if side == "BUY" else "BUY")
        altered = deepcopy(d)
        altered.order_admissions[0]["row"]["date"] = "2020-02-04"
        with pytest.raises(DomainError, match="PAPER_AD_ORDER_SCOPE"):
            order_permission(config, altered, security, side)
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
    scoped = service.view(sid)["config"]["execution_admission_scope"] == "per_order"
    if scoped:
        packet = ingest(service, sid, "2024-01-02", None)
        assert packet["packet_sha256"]
        with pytest.raises(DomainError) as caught:
            advance(service, sid, "2024-01-02", None)
        assert caught.value.code == "PAPER_AD_ORDER_NOT_ADMITTED"
    else:
        with pytest.raises(DomainError) as caught:
            ingest(service, sid, "2024-01-02", None)
        assert caught.value.code == "PAPER_AD_NOT_ADMITTED"
    assert service.view(sid)["head"] is None
    assert len(service.view(sid)["replay"]["delivery"]["received"]) == int(scoped)


def test_ad_explicit_model_choices_are_required(tmp_path):
    service, sid = bound(tmp_path, assumptions=[])
    assert not service.view(sid)["replay"]["preflight"]["ready"]
    with pytest.raises(DomainError) as caught:
        ingest(service, sid, "2020-02-03", None)
    assert caught.value.code == "PAPER_AD_NOT_ADMITTED"


def test_public_ingest_rejects_wrong_days_stocks_and_conflicting_duplicate(tmp_path):
    service, sid = bound(tmp_path)
    with TestClient(create_app(service.root)) as api:
        route = f"/api/v1/paper/sessions/{sid}/ingest"
        body = {"day": "2020-02-03", "expected_parent": None, "reason": "scope boundary"}
        for value, status in (
            ({**body, "day": "2020-02-02"}, 409),
            ({**body, "day": "2020-02-04"}, 409),
            ({**body, "day": "bad-date"}, 422),
            ({**body, "security": "999999.XSHE"}, 422),
            ({**body, "securities": ["000001.XSHE"]}, 422),
            ({**body, "expected_parent": "a" * 64}, 409),
        ):
            response = api.post(route, json=value)
            assert response.status_code == status
            assert response.json()["error"]["code"]
            assert not service.view(sid)["replay"]["delivery"]["received"]
        first = api.post(route, json=body)
        assert first.status_code == 200
        repeat = api.post(route, json=body)
        assert repeat.status_code == 200 and repeat.json()["already_received"]
        assert first.json()["packet_sha256"] == repeat.json()["packet_sha256"]
        changed = api.post(route, json={**body, "expected_parent": "a" * 64})
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "PAPER_DELIVERY_REQUEST_CONFLICT"
    assert service.view(sid)["head"] is None
    assert len(service.view(sid)["replay"]["delivery"]["received"]) == 1


def test_dev13_observation_unknowns_do_not_admit_other_day_or_side_orders(tmp_path):
    from ashare_quant.paper.contracts import Decision

    service, sid = bound(tmp_path, "w2024", strategy="trend")
    _, config = service.session(sid)
    if config.execution_admission_scope != "per_order":
        pytest.skip("Requires the fixed dev13 consumer contract")
    view = service.view(sid)
    assert not view["replay"]["delivery"]["ad_admission"]["ready"]
    assert view["replay"]["preflight"]["ready"]
    assert all(not x["blocking"] for x in view["replay"]["preflight"]["gaps"])
    ingest(service, sid, "2024-01-02", None)
    first = advance(service, sid, "2024-01-02", None)
    decision = Decision.model_validate_json(service.day(sid, "2024-01-02")["decision"])
    assert next(r for r in decision.rows if r.security == "600000.XSHG").is_st is None
    assert not decision.order_admissions and not first["native"]["trades"]
    parent = first["checkpoint_sha256"]
    ingest(service, sid, "2024-01-03", parent)
    second = advance(service, sid, "2024-01-03", parent)
    assert not second["native"]["trades"]
    assert not json.loads(service.day(sid, "2024-01-03")["decision"])["order_admissions"]
    assert second["native"]["settlement_count"] == 2


def test_reviewed_contract_rejects_old_event_pointer_and_missing_gap_reference(tmp_path):
    import hashlib
    import shutil

    from ashare_quant.paper.execution import contract_metadata

    configured = os.environ.get("ASHARE_PAPER_AD_PIN")
    if not configured:
        pytest.skip("Explicit sealed AD source required")
    source = Path(configured)
    spec = json.loads((source / "consumer.json").read_text())
    if not spec.get("consumer_contract"):
        pytest.skip("Requires the fixed dev13 consumer contract")
    metadata = contract_metadata(
        source / spec["consumer_contract"],
        spec["consumer_contract_sha"],
        source / "research/binding.json",
        source / "execution/binding.json",
        spec["ad_commit"],
    )
    assert metadata["references"]["existing_cash_event_contract"]["paths"][
        "research_event"
    ].endswith("#/actions/3")
    assert metadata["exact_gap_scope"]["fields_count"] == 696
    target = tmp_path / "contract"
    (target / "reports").mkdir(parents=True)
    original = source / spec["consumer_contract"]
    shutil.copy2(original, target / "consumer-contract.json")
    for rel in (
        "reports/cash-event-contract.json",
        "reports/impact-summary.json",
        "reports/exact-gap-scope.json",
    ):
        shutil.copy2(original.parent / rel, target / rel)
    cash_path = target / "reports/cash-event-contract.json"
    cash = json.loads(cash_path.read_text())
    cash["paths"]["research_event"] = cash["paths"]["research_event"].replace(
        "#/actions/3", "#/actions/0"
    )
    cash_path.write_text(json.dumps(cash))
    with pytest.raises(DomainError, match="Cash event pointer"):
        contract_metadata(
            target / "consumer-contract.json",
            spec["consumer_contract_sha"],
            source / "research/binding.json",
            source / "execution/binding.json",
            spec["ad_commit"],
        )
    shutil.copy2(original.parent / "reports/cash-event-contract.json", cash_path)
    cpath = target / "consumer-contract.json"
    bad = json.loads(cpath.read_text())
    bad["unresolved"]["details"] = "exact-gap-scope.json"
    cpath.write_text(json.dumps(bad))
    with pytest.raises(DomainError):
        contract_metadata(
            cpath,
            hashlib.sha256(cpath.read_bytes()).hexdigest(),
            source / "research/binding.json",
            source / "execution/binding.json",
            spec["ad_commit"],
        )
