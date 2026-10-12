"""Source-owner corpus into native two-day fixtures; not real calendar evidence."""

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.daily_portfolio import signals
from ashare_quant.m2_jobs.files import digest, sha
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_adapter import (
    UNIVERSE,
    OwnerReader,
    decision_from_owner,
    scoped_action_effect,
)
from ashare_quant.paper.online_contracts import OnlineSession
from ashare_quant.paper.risk import profiles


def bound_fact(day="2026-10-13"):
    proof = dict(
        baseline_sha256=digest("fixture-baseline"),
        security=UNIVERSE[0],
        day=day,
        sources={"fixture": digest("source")},
        catalog_count=1,
        reviewed_text_count=1,
        reviewed_at="2026-10-12T18:00:00+08:00",
        fresh_check_completed_at=day + "T08:55:00+08:00",
        basis="bounded_reviewed_issuer_corpus_and_unchanged_current_sources",
    )
    return dict(
        security=UNIVERSE[0],
        actions_known_absent=False,
        action_scope=dict(
            from_day="2026-10-13",
            through_day="2026-10-14",
            security=UNIVERSE[0],
            price_share_code_review=dict(status="proven", source_sha256=digest(proof), **proof),
        ),
        cash_inventory=dict(
            cash_history_complete=True,
            unresolved=[],
            events=[
                dict(
                    type="cash_dividend",
                    record_date="2026-07-15",
                    ex_date="2026-07-16",
                    pay_date="2026-07-16",
                )
            ],
        ),
    )


@pytest.mark.parametrize(
    "damage", ["absent_baseline", "security", "day", "digest", "genesis", "record", "ex"]
)
def test_bound_review_rejects_wrong_identity_entitlement_or_tamper(damage):
    f = bound_fact()
    p = f["action_scope"]["price_share_code_review"]
    state = {"days": {}}
    if damage == "absent_baseline":
        p.pop("baseline_sha256")
    elif damage == "security":
        f["security"] = UNIVERSE[1]
    elif damage == "day":
        p["day"] = "2026-10-14"
    elif damage == "digest":
        p["sources"] = {}
    elif damage == "genesis":
        state["days"] = {"2026-01-01": {}}
    elif damage == "record":
        f["cash_inventory"]["events"][0]["record_date"] = "2026-10-13"
    elif damage == "ex":
        f["cash_inventory"]["events"][0]["ex_date"] = "2026-10-13"
    with pytest.raises(DomainError):
        scoped_action_effect(state, f, "2026-10-13", require_binding=True)


def test_baseline_continuity_is_bound_to_prior_native_decision():
    f = bound_fact()
    _, evidence = scoped_action_effect({"days": {}}, f, "2026-10-13", require_binding=True)
    state = {
        "days": {
            "2026-10-13": {
                "decision": {"strategy_receipt": {"action_adjudications": {UNIVERSE[0]: evidence}}}
            }
        }
    }
    f = bound_fact("2026-10-14")
    assert (
        scoped_action_effect(state, f, "2026-10-14", require_binding=True)[0] == "zero_entitlement"
    )
    p = f["action_scope"]["price_share_code_review"]
    p["baseline_sha256"] = digest("different-baseline")
    p["source_sha256"] = digest(
        {k: v for k, v in p.items() if k not in {"status", "source_sha256"}}
    )
    with pytest.raises(DomainError, match="PAPER_ACTION_BASELINE_CHANGED"):
        scoped_action_effect(state, f, "2026-10-14", require_binding=True)


def test_owner_pin_includes_corpus_bytes_not_only_python(tmp_path):
    package = tmp_path / "src" / "ashare_data"
    evidence = package / "pilot_evidence" / "blobs"
    evidence.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (evidence / "source").write_bytes(b"original")
    reader = OwnerReader("python", tmp_path / "src", tmp_path, tmp_path)
    before = reader.pin()
    (evidence / "source").write_bytes(b"changed")
    assert reader.pin() != before


def test_real_owner_corpus_drives_two_day_native_fixture(tmp_path):
    ad_source = os.environ.get("ASHARE_CALENDAR_AD_SOURCE")
    ad_python = os.environ.get("ASHARE_CALENDAR_AD_PYTHON")
    if not ad_source or not ad_python:
        pytest.skip("Explicit AD worktree and interpreter required for cross-owner fixture")
    # Run AD in isolation with an explicitly controlled clock and network-free
    # fixtures. It re-reads and verifies every packet through the production AD
    # reader. Returned packets are consumed only by calendar_fixture identities.
    script = r"""
import sys,json
from pathlib import Path
from datetime import datetime
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1]).parent/'tests')]
import pytest
from ashare_data import phase_observation as phase
from test_action_scope import issuer_sources
out={}
for day in ['2026-10-13','2026-10-14']:
    mp=pytest.MonkeyPatch()
    _,original=issuer_sources(mp)
    mp.setattr(phase,'now',lambda:datetime.fromisoformat(day+'T08:55:00+08:00'))
    def fetch(url,referer):
        body=original(url.replace('end_time=2026-10-14','end_time=2026-10-13'),referer)
        if day.endswith('14'):
            if 'hq.sinajs' in url:body=body.replace(b'2026-10-13',b'2026-10-14')
            if 'push2.eastmoney' in url:
                v=json.loads(body);v['data']['f86']+=86400;body=json.dumps(v).encode()
        return body
    mp.setattr(phase,'fetch',fetch)
    root=Path(sys.argv[2])/day
    attempt=phase.capture(root,'admission',day)
    assert attempt['status']=='observed',attempt
    out[day]={'capture_id':attempt['capture_id'],'packet':phase.read(root,attempt['capture_id'])}
    mp.undo()
print(json.dumps(out))
"""
    result = subprocess.run(
        [ad_python, "-I", "-B", "-c", script, ad_source, str(tmp_path / "owner")],
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
    )
    packets = json.loads(result.stdout)
    config = OnlineSession(
        account_id="explicit-owner-to-native-fixture",
        data_mode="calendar_fixture",
        universe=UNIVERSE,
        initial_cash="1000000",
        strategy_version=sha(Path(signals.__file__).read_bytes()),
        scenario_manifest_sha256=digest("not-real-calendar-observation"),
        admission_reader_sha256=digest("isolated-fixture-AD-reader"),
        risk=profiles()["target_then_native_cash_v1"],
    )
    service = OnlineService(tmp_path / "native")
    service.create(config)
    reports = []
    for prior, day, after in [
        ("2026-10-12", "2026-10-13", "2026-10-14"),
        ("2026-10-13", "2026-10-14", "2026-10-15"),
    ]:
        state, _ = service._load()
        daily = dict(
            signal_day=prior,
            signal_ready=True,
            revisions=[],
            completed_at=prior + "T17:45:00+08:00",
            calendar=dict(
                next_open=day,
                open_days=[prior, day, after],
                source=dict(observed_at=prior + "T17:45:00+08:00"),
            ),
            securities={
                s: dict(
                    rows=[dict(close=p)] * 120, source=dict(observed_at=prior + "T17:45:00+08:00")
                )
                for s, p in zip(UNIVERSE, ("10", "20", "30"), strict=True)
            },
        )

        class FixtureReader:
            def read(self, config, kind, capture):
                return daily if kind == "daily" else packets[day]["packet"]

        d = decision_from_owner(
            config,
            state,
            reports[-1] if reports else None,
            FixtureReader(),
            digest(daily),
            packets[day]["capture_id"],
        )
        value = d.model_dump(mode="json")
        value.update(
            evidence_kind="calendar_fixture",
            clock_basis="fixture_clock",
            received_at=day + "T09:00:01+08:00",
        )
        row = service.prepare_fixture(value)
        assert service.prepare_fixture(value) == row
        for receipt in value["strategy_receipt"]["action_adjudications"].values():
            assert receipt["genesis"] == "2026-10-13"
        assert all(
            r["action_effect"] == "zero_entitlement" and not r["actions_known_absent"]
            for r in value["rows"]
        )
        opening = dict(
            trading_date=day,
            intent_id=row["intent_id"],
            evidence_kind="calendar_fixture",
            clock_basis="fixture_clock",
            received_at=day + "T09:31:00+08:00",
            rows=[
                dict(
                    security=s,
                    trading_date=day,
                    open=p,
                    observed_at=day + "T09:30:59+08:00",
                    source_as_of=day + "T09:30:58+08:00",
                    raw_sha256=digest([s, p]),
                )
                for s, p in zip(UNIVERSE, ("10", "20", "30"), strict=True)
            ],
        )
        service.deliver_fixture("opening", opening)
        opened = service.advance(day, "opening")
        # Exercise restart/missing mutable checkpoint with the genuine owner
        # action receipt; published opening remains the native continuation.
        (service.root / day / "native.chk").unlink()
        service = OnlineService(service.root)
        close = dict(
            trading_date=day,
            intent_id=row["intent_id"],
            evidence_kind="calendar_fixture",
            clock_basis="fixture_clock",
            received_at=day + "T17:45:00+08:00",
            deadline=day + "T18:45:00+08:00",
            data_manifest_sha256=digest([day, "close"]),
            rows=[
                dict(
                    security=s,
                    trading_date=day,
                    open=p,
                    high=p,
                    low=p,
                    close=p,
                    volume=100000000,
                    observed_at=day + "T17:45:00+08:00",
                    complete=True,
                    source_watermark=day,
                    selected_watermark=day,
                )
                for s, p in zip(UNIVERSE, ("10", "20", "30"), strict=True)
            ],
        )
        service.deliver_fixture("closing", close)
        report = service.advance(day, "closing")
        assert report["native"]["trades"] == opened["native"]["trades"]
        assert service.advance(day, "closing") == report
        assert report["native"]["settlement_count"] == len(reports) + 1
        reports.append(report)
    assert len(reports[0]["native"]["trades"]) == 3
    assert reports[1]["native"]["trades"] == []
    first, second = (r["native"]["account"] for r in reports)
    assert first["total_cash"] == second["total_cash"] and first["nav"] == second["nav"]
    for security in UNIVERSE:
        assert first["positions"][security]["quantity"] == second["positions"][security]["quantity"]
        assert (
            second["positions"][security]["sellable"] == second["positions"][security]["quantity"]
        )
    assert Decimal(reports[1]["native"]["account"]["total_cash"]) > 0
    assert service.status()["real_calendar_observation"] is False
    (tmp_path / "owner-native-fixture-summary.json").write_text(
        json.dumps(
            dict(
                evidence_kind="calendar_fixture",
                real_calendar_observation=False,
                owner_packets={d: p["capture_id"] for d, p in packets.items()},
                checkpoints=[r["checkpoint_sha256"] for r in reports],
                accounts=[r["native"]["account"] for r in reports],
            ),
            indent=2,
        )
    )
