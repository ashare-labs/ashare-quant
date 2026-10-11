"""Shared strategy receipts, single native book, and visible integration gates."""

import json
import runpy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.daily_portfolio.signals import decide
from ashare_quant.m2_jobs.files import digest
from ashare_quant.paper.api import create_app
from ashare_quant.paper.replay import advance, bind, build, close_for
from ashare_quant.paper.service import PaperService

ROOT = Path(__file__).resolve().parents[2]
RISK = {"max_gross": "1", "cash_buffer": "0"}


def source(tmp_path, change=None):
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    manifest = json.loads((ROOT / "examples/daily_portfolio/manifest.json").read_text())
    manifest["execution"]["initial_cash"] = "10000"
    generate = runpy.run_path(str(ROOT / "scripts/verify_daily_portfolio.py"))["generate"]
    doc = generate(manifest, constant=True)
    for row in doc["daily"]:
        row["suspended"] = False
    if change:
        change(doc)
    m, p = root / "manifest.json", root / "input.json"
    m.write_text(json.dumps(manifest))
    p.write_text(json.dumps(doc))
    return m, p


def bound(tmp_path, strategy="equal-weight", change=None, risk=RISK):
    m, p = source(tmp_path, change)
    service = PaperService(tmp_path / "store")
    result = bind(service, m, p, strategy, "w2020", tmp_path.name, risk=risk)
    return service, result["session_id"]


@pytest.mark.parametrize("strategy", ["equal-weight", "momentum", "trend"])
def test_shared_code_receipts_and_three_native_continuations(tmp_path, strategy):
    service, sid = bound(tmp_path, strategy)
    parent = None
    reports = []
    for n, day in enumerate(("2020-02-03", "2020-02-04", "2020-02-05")):
        decision, inputs = build(service, sid, day, parent)
        receipt = decision.strategy_receipt
        assert receipt["history_sha256"] == digest(inputs.history(day))
        if receipt["scheduled"]:
            assert receipt["signal"] == decide(
                strategy, inputs.history(day), receipt["previous_equity"]
            )
        if reports:
            assert receipt["previous_equity"] == reports[-1]["native"]["account"]["nav"]
        report = advance(service, sid, day, parent)
        assert advance(service, sid, day, parent) == report
        assert report["native"]["cursor"] == 4 * (n + 1)
        assert report["native"]["settlement_count"] == n + 1
        assert all(e["at"].startswith(day) for e in report["native"]["events"])
        reports.append(report)
        parent = report["checkpoint_sha256"]
    fills = [t for r in reports for t in r["native"]["trades"]]
    assert len(fills) == len({t["id"] for t in fills})
    if strategy == "equal-weight":
        assert len(fills) == 8
        assert Decimal(reports[-1]["native"]["account"]["total_cash"]) == 1960
    assert service.view(sid)["report"] == reports[-1]


def test_recovered_strategy_request_does_not_execute_twice(tmp_path):
    service, sid = bound(tmp_path)
    with pytest.raises(DomainError, match="Native worker exited -9"):
        advance(service, sid, "2020-02-03", None, fault="inside_native_trade")
    assert service.status(sid)["session"]["head"] is None
    service.control(sid, "resume", None, "test recovery of fixed strategy receipt")
    report = advance(service, sid, "2020-02-03", None)
    assert len(report["native"]["trades"]) == 8
    assert len({t["id"] for t in report["native"]["trades"]}) == 8
    assert Decimal(report["native"]["account"]["total_cash"]) == 1960
    assert advance(service, sid, "2020-02-03", None) == report


def test_default_risk_is_not_relaxed_for_shared_strategy(tmp_path):
    service, sid = bound(tmp_path, risk={})
    with pytest.raises(DomainError) as rejected:
        advance(service, sid, "2020-02-03", None)
    assert rejected.value.code == "PAPER_GROSS_LIMIT"
    view = service.view(sid)
    assert view["head"] is None and view["latest_account"] is None
    assert view["report"] is None and view["days"][0]["status"] == "recovery_required"
    assert view["days"][0]["error"]["message"] == "独立风控拒绝当前目标；账户未提交。"
    assert not list((service.root / sid).glob("*/native.chk"))


def test_later_risk_rejection_preserves_committed_native_book(tmp_path):
    def change(doc):
        for row in doc["daily"]:
            if row["date"] == "2020-02-04":
                row["upper_limit"] = "30"

    service, sid = bound(tmp_path, change=change)
    first = advance(service, sid, "2020-02-03", None)
    previous = service.root / sid / "2020-02-03/native.chk"
    original = previous.read_bytes()
    with pytest.raises(DomainError) as rejected:
        advance(service, sid, "2020-02-04", first["checkpoint_sha256"])
    assert rejected.value.code == "PAPER_GROSS_LIMIT"
    assert previous.read_bytes() == original
    assert service.status(sid)["session"]["head"] == first["checkpoint_sha256"]
    assert service.report(sid, "2020-02-03") == first
    assert service.view(sid)["latest_account"] == first["native"]["account"]


def test_unscheduled_day_holds_actual_positions_not_unfilled_targets(tmp_path):
    def change(doc):
        for row in doc["daily"]:
            if row["date"] == "2020-02-03":
                row.update(open="11", high="11")

    service, sid = bound(tmp_path, change=change)
    first = advance(service, sid, "2020-02-03", None)
    assert not first["native"]["trades"] and len(first["native"]["target_gaps"]) == 8
    following = advance(service, sid, "2020-02-04", first["checkpoint_sha256"])
    assert not following["strategy_receipt"]["scheduled"]
    assert set(following["strategy_receipt"]["signal"]["targets"].values()) == {0}
    assert not following["native"]["orders"] and not following["native"]["trades"]


def test_http_cannot_replace_bound_receipt_or_close(tmp_path):
    service, sid = bound(tmp_path)
    d, inputs = build(service, sid, "2020-02-03", None)
    altered = d.model_dump(mode="json")
    altered["targets"]["000001.XSHE"] += 100
    with TestClient(create_app(service.root)) as api:
        base = f"/api/v1/paper/sessions/{sid}"
        assert api.get("/api/v1/paper/sessions").json()["sessions"][0]["id"] == sid
        assert (
            api.post(base + "/decisions", json=altered).json()["error"]["code"]
            == "PAPER_STRATEGY_RECEIPT_CHANGED"
        )
        intent = api.post(base + "/decisions", json=d.model_dump(mode="json")).json()["intent_id"]
        _, cfg = service.session(sid)
        close = close_for(cfg, d, inputs, intent).model_dump(mode="json")
        close["rows"][0]["close"] = "10.01"
        assert (
            api.post(base + "/closes", json=close).json()["error"]["code"]
            == "PAPER_REPLAY_CLOSE_CHANGED"
        )
        payload = {"day": "2020-02-03", "expected_parent": None, "reason": "local acceptance"}
        first = api.post(base + "/advance", json=payload)
        assert first.status_code == 200, first.text
        assert api.post(base + "/advance", json=payload).json() == first.json()
        view = api.get(base + "/view").json()
        assert view["report"]["checkpoint_sha256"] == view["head"]
        assert view["continuous_paper_accepted"] is False
        assert first.headers["cache-control"] == "no-store"


def test_binding_tamper_hides_results_and_prevents_execution(tmp_path):
    service, sid = bound(tmp_path)
    path = service.root / sid / "replay/input.json"
    path.write_bytes(path.read_bytes() + b" ")
    with TestClient(create_app(service.root)) as api:
        response = api.get(f"/api/v1/paper/sessions/{sid}/view")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PAPER_REPLAY_INPUT_CHANGED"
    assert service.status(sid)["session"]["head"] is None


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("missing", "PAPER_REPLAY_DATA_MISSING"),
        ("action", "PAPER_ACTION_TIMING_UNSUPPORTED"),
    ],
)
def test_owner_gap_and_action_timing_are_visible_without_ledger_write(tmp_path, scenario, code):
    def change(doc):
        if scenario == "missing":
            doc["daily"] = [
                r
                for r in doc["daily"]
                if not (r["date"] == "2020-02-03" and r["security"] == "000651.XSHE")
            ]
        else:
            doc["actions"] = [
                {
                    "security": "000651.XSHE",
                    "kind": "cash_dividend",
                    "record_date": "2020-02-03",
                    "ex_date": "2020-02-04",
                    "pay_date": "2020-02-04",
                    "cash_per_share": "1",
                }
            ]

    service, sid = bound(tmp_path, change=change)
    with pytest.raises(DomainError) as rejected:
        advance(service, sid, "2020-02-03", None)
    assert rejected.value.code == code
    status = service.status(sid)
    assert status["session"]["head"] is None and not status["days"]
    assert status["events"][-1]["details"]["code"] == code


def test_current_and_future_prices_do_not_enter_signal_receipt(tmp_path):
    service, sid = bound(tmp_path)
    original, _ = build(service, sid, "2020-02-03", None)
    second = tmp_path / "second"
    second.mkdir()

    def change(doc):
        for row in doc["daily"]:
            if row["date"] >= "2020-02-03":
                row.update(open="15", high="15", low="15", close="15")

    other, other_sid = bound(second, change=change)
    changed, _ = build(other, other_sid, "2020-02-03", None)
    assert changed.targets == original.targets
    for key in ("signal", "history_sha256", "information_cutoff", "previous_equity"):
        assert changed.strategy_receipt[key] == original.strategy_receipt[key]
    assert changed.data_manifest_sha256 != original.data_manifest_sha256


def test_real_mode_requires_owner_role_and_explicit_acceptance(tmp_path):
    m, p = source(tmp_path)
    doc = json.loads(p.read_text())
    doc.update(
        real_market_data=True,
        producer={"name": "ashare-data"},
        state_mode="retrospective_model",
        owner_descriptor={
            "execution_permission": False,
            "historical_pit": False,
            "role": "daily_strategy_retrospective_research",
        },
    )
    # Deliberately no real owner receipt: a flag cannot turn a fixture into trusted data.
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        bind(
            PaperService(tmp_path / "store"),
            m,
            p,
            "trend",
            "w2020",
            tmp_path.name,
            accept_model=True,
        )
