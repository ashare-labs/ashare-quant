"""Explicit staged fixtures; none count as real calendar acceptance."""

import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_contracts import OnlineSession

from .fixtures import close, config, decision

MODE = {"evidence_kind": "calendar_fixture", "clock_basis": "fixture_clock"}


def setup(tmp_path):
    value = config(tmp_path.name).model_dump(mode="json")
    value.update(
        model="prior-close-standing-intent-observed-open.v1",
        data_mode="calendar_fixture",
        admission_reader_sha256=digest("explicit-test-owner"),
    )
    session = OnlineSession.model_validate(value)
    service = OnlineService(tmp_path)
    service.create(session)
    return service, session


def prepare(service, session, index=0, targets=(100, 100), parent=None):
    d = decision(session, index, targets, parent)
    d.update(MODE)
    return service.prepare_fixture(d)


def opening(row, prices=("10", "20")):
    day = row["day"]
    return {
        "trading_date": day,
        "intent_id": row["intent_id"],
        **MODE,
        "received_at": day + "T09:31:00+08:00",
        "rows": [
            {
                "security": s,
                "trading_date": day,
                "open": p,
                "observed_at": day + "T09:30:59+08:00",
                "source_as_of": day + "T09:30:58+08:00",
                "raw_sha256": digest([s, p]),
            }
            for s, p in zip(session_universe(row), prices, strict=True)
        ],
    }


def session_universe(row):
    return [r["security"] for r in row["decision"]["rows"]]


def closing(row):
    return {**close(row["day"], row["intent_id"]), **MODE}


def start(service, row, **kwargs):
    service.deliver_fixture("opening", opening(row))
    return service.advance(row["day"], "opening", **kwargs)


def finish(service, row, **kwargs):
    service.deliver_fixture("closing", closing(row))
    return service.advance(row["day"], "closing", **kwargs)


def test_separate_arrivals_native_account_no_early_settlement(tmp_path):
    service, session = setup(tmp_path)
    head = None
    reports = []
    for index, targets in enumerate(((100, 100), (0, 100), (100, 0))):
        row = prepare(service, session, index, targets, head)
        opened = start(service, row)
        assert opened["native"]["cursor"] == index * 4 + 2
        assert opened["native"]["settlement_count"] == index
        assert opened["native"]["close_sha256"] is None
        assert all(t["at"].endswith("09:31:00") for t in opened["native"]["trades"])
        request = json.loads((tmp_path / row["day"] / "opening-request.json").read_bytes())
        assert request["close"] is None
        assert set(request["opening"]["rows"][0]) == {
            "security",
            "trading_date",
            "open",
            "observed_at",
            "source_as_of",
            "raw_sha256",
        }
        service = OnlineService(tmp_path)  # both the controller and native worker have restarted
        report = finish(service, row)
        head = report["checkpoint_sha256"]
        reports.append(report)
        assert report["native"]["cursor"] == (index + 1) * 4
        assert report["native"]["settlement_count"] == index + 1
        assert report["native"]["events"][-1]["at"].endswith("16:10:00")
        assert report["native"]["trades"] == opened["native"]["trades"]
        assert service.advance(row["day"], "opening") == opened
        assert service.advance(row["day"], "closing") == report
    assert Decimal(reports[0]["native"]["account"]["total_cash"]) == 6990
    assert Decimal(reports[-1]["native"]["account"]["total_cash"]) == 8972
    assert reports[1]["native"]["before"]["positions"]["000001.XSHE"]["sellable"] == 100
    ids = [t["id"] for r in reports for t in r["native"]["trades"]]
    assert len(ids) == len(set(ids)) == 5
    assert service.status()["real_calendar_observation"] is False


@pytest.mark.parametrize(
    "phase,fault",
    [
        ("opening", "inside_native_trade"),
        ("opening", "event_committed:2"),
        ("opening", "after_opening_checkpoint"),
        ("closing", "inside_native_settlement"),
        ("closing", "after_complete_checkpoint"),
    ],
)
def test_kill_and_resume_each_phase_no_duplicate_fills(tmp_path, phase, fault):
    service, session = setup(tmp_path)
    row = prepare(service, session)
    if phase == "closing":
        start(service, row)
    with pytest.raises(DomainError, match="exited -9"):
        (start if phase == "opening" else finish)(service, row, fault=fault)
    assert service.status()["days"][row["day"]]["recovery_required"]
    with pytest.raises(DomainError, match="PAPER_RESUME_REQUIRED"):
        service.advance(row["day"], phase)
    OnlineService(tmp_path).resume("explicit recovery of immutable inputs")
    report = service.advance(row["day"], phase)
    if phase == "opening":
        report = finish(service, row)
    assert len(report["native"]["trades"]) == 2
    assert Decimal(report["native"]["account"]["total_cash"]) == 6990
    assert report["native"]["settlement_count"] == 1


def test_source_revision_future_ohlc_and_close_before_open_refused(tmp_path):
    service, session = setup(tmp_path)
    row = prepare(service, session)
    with pytest.raises(DomainError, match="PAPER_OPENING_PENDING"):
        service.deliver_fixture("closing", closing(row))
    from pydantic import ValidationError

    future = opening(row)
    future["rows"][0]["close"] = "12"
    with pytest.raises(ValidationError):
        service.deliver_fixture("opening", future)
    start(service, row)
    altered = closing(row)
    altered["rows"][0].update(open="10.1", high="10.1")
    with pytest.raises(DomainError, match="PAPER_OPENING_PRICE_REVISION"):
        service.deliver_fixture("closing", altered)
    assert service.status()["days"][row["day"]]["status"] == "awaiting_close"


def test_no_manual_boolean_bypass_for_observed_session(tmp_path):
    service, session = setup(tmp_path / "fixture")
    observed = OnlineService(tmp_path / "observed")
    observed.create(session.model_copy(update={"data_mode": "calendar_observed"}))
    with pytest.raises(DomainError, match="PAPER_OWNER_ADAPTER_REQUIRED"):
        observed.prepare_fixture(decision(session, 0, (100, 100)))


def test_risk_rejection_does_not_advance_account_head(tmp_path):
    service, session = setup(tmp_path)
    row = prepare(service, session, targets=(900, 900))
    with pytest.raises(DomainError, match="Native worker exited"):
        start(service, row)
    state = service.status()
    assert state["head"] is None and state["last_day"] is None
    assert state["days"][row["day"]]["status"] == "failed"
    assert state["latest_account"] is None
    logs = list((tmp_path / row["day"]).glob("native-*.log"))
    assert "PAPER_CONCENTRATION_LIMIT" in logs[0].read_text()


@pytest.mark.parametrize("point", ["spawned", "before_publish"])
def test_controller_sigkill_preserves_phase_fence_and_checkpoint(tmp_path, point):
    service, session = setup(tmp_path)
    row = prepare(service, session)
    service.deliver_fixture("opening", opening(row))
    boot = """
import json,os,signal,sys
from ashare_quant.paper.online import OnlineService
root,day,point=json.loads(sys.argv[1])
def stage(where, child):
    if where == point:
        os.kill(os.getpid(), signal.SIGKILL)
OnlineService(root).advance(day,'opening',stage=stage)
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", boot, json.dumps([str(tmp_path), row["day"], point])],
        capture_output=True,
        timeout=25,
    )
    assert process.returncode == -signal.SIGKILL, process.stderr.decode()
    assert service.status()["days"][row["day"]]["recovery_required"]
    deadline = time.monotonic() + 8
    while True:
        try:
            service.resume("controlled controller SIGKILL recovery")
            break
        except DomainError as exc:
            assert exc.code == "M2_JOB_WRITER_BUSY" and time.monotonic() < deadline
            time.sleep(0.1)
    opened = service.advance(row["day"], "opening")
    report = finish(service, row)
    assert report["native"]["trades"] == opened["native"]["trades"]
    assert len(report["native"]["trades"]) == 2
    assert Decimal(report["native"]["account"]["total_cash"]) == 6990


def test_actual_cash_rejects_second_order_retains_first_native_fill(tmp_path):
    from ashare_quant.paper.contracts import RiskPolicy

    _, session = setup(tmp_path / "spec")
    session = session.model_copy(
        update={
            "risk": RiskPolicy(
                profile="target_then_native_cash_v1",
                max_single="0.6",
                execution_max_single="1",
                max_gross="1",
                cash_buffer="0",
                max_order_value="1000000",
                max_order_nav_ratio="1",
                max_turnover="2",
            )
        }
    )
    service = OnlineService(tmp_path / "account")
    service.create(session)
    row = prepare(service, session, targets=(600, 200))
    opened = start(service, row)
    assert len(opened["native"]["trades"]) == 1
    assert opened["native"]["refusals"][0]["code"] == "PAPER_CASH_BUFFER"
    report = finish(service, row)
    assert Decimal(report["native"]["account"]["total_cash"]) == 3995
    assert report["native"]["account"]["positions"]["000001.XSHE"]["quantity"] == 600
    assert report["native"]["target_gaps"] == {"600000.XSHG": 200}


def test_real_clock_deadlines_do_not_use_fixture_timestamp(tmp_path, monkeypatch):
    from ashare_quant.paper import online

    _, session = setup(tmp_path)
    session = session.model_copy(update={"data_mode": "calendar_observed"})
    monkeypatch.setattr(online, "now", lambda: datetime.fromisoformat("2026-10-13T23:00:00+08:00"))
    with pytest.raises(DomainError, match="PAPER_REAL_WINDOW_CLOSED"):
        OnlineService._window(
            session,
            "2026-10-13",
            datetime.min.time().replace(hour=9, minute=30),
            datetime.min.time().replace(hour=9, minute=35),
            datetime.fromisoformat("2026-10-13T09:31:00+08:00"),
        )


def test_committed_native_evidence_tamper_visible_and_next_day_blocked(tmp_path):
    service, session = setup(tmp_path)
    row = prepare(service, session)
    start(service, row)
    report = finish(service, row)
    path = tmp_path / row["day"] / "closing.chk"
    path.write_bytes(path.read_bytes()[:-3] + b"bad")
    state = service.status()
    assert state["days"][row["day"]]["status"] == "evidence_conflict"
    assert state["latest_account"] is None
    with pytest.raises(DomainError, match="PAPER_CHECKPOINT_CHANGED"):
        prepare(service, session, 1, parent=report["checkpoint_sha256"])


def test_later_close_changes_only_valuation_and_not_prior_open_fills(tmp_path):
    service, session = setup(tmp_path)
    row = prepare(service, session)
    opened = start(service, row)
    assert Decimal(opened["native"]["account"]["nav"]) == 9990
    value = closing(row)
    for r, price in zip(value["rows"], ("10.5", "21"), strict=True):
        r.update(close=price, high=price)
    service.deliver_fixture("closing", value)
    report = service.advance(row["day"], "closing")
    assert Decimal(report["native"]["account"]["nav"]) == 10140
    assert report["native"]["trades"] == opened["native"]["trades"]
    assert service.report(row["day"], "opening") == opened


def test_historical_controller_rejects_online_model_instance_before_insertion(tmp_path):
    from ashare_quant.paper.service import PaperService

    _, session = setup(tmp_path / "online")
    old = PaperService(tmp_path / "historical")
    with pytest.raises(DomainError, match="PAPER_SESSION_MODE_UNSUPPORTED"):
        old.create(session)
    assert old.catalogue()["sessions"] == []


def test_owner_builder_uses_native_equity_and_holds_actual_fills_fixture(tmp_path):
    from pathlib import Path

    from ashare_quant.daily_portfolio import signals
    from ashare_quant.m2_jobs.files import sha
    from ashare_quant.paper.online_adapter import UNIVERSE, decision_from_owner

    _, session = setup(tmp_path)
    session = session.model_copy(
        update={"universe": UNIVERSE, "strategy_version": sha(Path(signals.__file__).read_bytes())}
    )
    previous, day, after = "2026-10-13", "2026-10-14", "2026-10-15"
    prior_at = previous + "T17:45:00+08:00"
    fake_daily = dict(
        signal_day=previous,
        completed_at=prior_at,
        signal_ready=True,
        revisions=[],
        calendar=dict(
            next_open=day, open_days=[previous, day, after], source=dict(observed_at=prior_at)
        ),
        securities={
            s: dict(rows=[{"close": "10"}] * 120, source=dict(observed_at=prior_at))
            for s in UNIVERSE
        },
    )
    fake_admission = dict(
        kind="admission",
        day=day,
        completed_at=day + "T08:55:00+08:00",
        assessment=dict(
            execution_permission=True,
            blockers=[],
            rows=[
                dict(
                    security=s,
                    eligible=True,
                    suspended=False,
                    is_st=False,
                    lower_limit="9",
                    upper_limit="11",
                    rule_version="FIXTURE",
                    actions_known_absent=True,
                )
                for s in UNIVERSE
            ],
        ),
    )

    class FixtureReader:
        def read(self, config, kind, capture):
            return fake_daily if kind == "daily" else fake_admission

    positions = {s: {"quantity": q} for s, q in zip(UNIVERSE, (100, 200, 0), strict=True)}
    native = dict(native=dict(account=dict(nav="345678.90", positions=positions)))
    built = decision_from_owner(
        session,
        {"head": digest("fixture-parent")},
        native,
        FixtureReader(),
        digest("daily"),
        digest("admission"),
    )
    assert built.strategy_receipt["equity"] == "345678.90"
    assert built.strategy_receipt["scheduled"] is False
    assert built.targets == {s: p["quantity"] for s, p in positions.items()}
    fake_admission["assessment"]["execution_permission"] = False
    fake_admission["assessment"]["blockers"] = ["ALL_ACTION_TYPES_COVERAGE_UNVERIFIED"]
    with pytest.raises(DomainError, match="ALL_ACTION_TYPES_COVERAGE_UNVERIFIED"):
        decision_from_owner(
            session, {"head": None}, None, FixtureReader(), digest("daily"), digest("admission")
        )
