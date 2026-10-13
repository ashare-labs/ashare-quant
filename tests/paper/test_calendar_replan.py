"""Explicit plan migration and no silent first-day skip; fixture accounts only."""

import copy
import json

import pytest
from pydantic import ValidationError

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest, sha
from ashare_quant.paper.online_contracts import CalendarReplan

from .test_online import prepare, setup


def plan_for(config, **changes):
    value = dict(
        first_window=["2026-10-14", "2026-10-15"],
        scope=list(config.universe),
        previous_scenario_manifest_sha256=config.scenario_manifest_sha256,
        admission_reader_sha256=digest("new-owner"),
        action_baseline_sha256=digest("fixture-baseline"),
        calendar_source_sha256=digest("calendar"),
        source_state_manifest_sha256=digest("sources"),
    )
    value.update(changes)
    return CalendarReplan.model_validate(value)


def test_migration_binds_plan_and_preserves_all_economic_terms(tmp_path):
    service, config = setup(tmp_path)
    raw = service.path.read_bytes()
    old = json.loads(raw)
    plan = plan_for(config)
    result = service.migrate_empty(
        plan.admission_reader_sha256, sha(raw), "explicit calendar replan", plan=plan
    )
    assert result["calendar_plan"] == plan.model_dump(mode="json")
    assert result["config"]["scenario_manifest_sha256"] == digest(plan)
    assert (tmp_path / "migrations" / (sha(raw) + ".json")).read_bytes() == raw
    assert json.loads(
        (tmp_path / "migrations" / ("plan-" + digest(plan) + ".json")).read_bytes()
    ) == plan.model_dump(mode="json")
    before = old["config"]
    after = copy.deepcopy(result["config"])
    for key in ("scenario_manifest_sha256", "admission_reader_sha256"):
        after[key] = before[key]
    assert after == before and not result["days"] and result["head"] is None
    assert result["events"][:-1] == old["events"]
    with pytest.raises(DomainError, match="PAPER_MIGRATION_STATE_CHANGED"):
        service.migrate_empty(plan.admission_reader_sha256, sha(raw), "stale replay", plan=plan)


@pytest.mark.parametrize(
    "damage",
    ["old_plan", "owner", "scope", "prepared", "orphan_day", "orphan_checkpoint", "orphan_request"],
)
def test_replan_rejects_wrong_identity_or_nonempty_account(tmp_path, damage):
    service, config = setup(tmp_path)
    p = plan_for(config).model_dump(mode="json")
    if damage == "old_plan":
        p["previous_scenario_manifest_sha256"] = digest("wrong")
    if damage == "owner":
        p["admission_reader_sha256"] = digest("wrong")
    if damage == "scope":
        p["scope"] = ["600000.XSHG"]
    if damage == "prepared":
        prepare(service, config)
    if damage == "orphan_day":
        (tmp_path / "2026-10-14").mkdir()
    if damage == "orphan_checkpoint":
        (tmp_path / "native.chk").write_bytes(b"orphan")
    if damage == "orphan_request":
        (tmp_path / "request.json").write_text("{}")
    raw = service.path.read_bytes()
    with pytest.raises(DomainError):
        service.migrate_empty(digest("new-owner"), sha(raw), "no clearing", plan=p)
    assert service.path.read_bytes() == raw


@pytest.mark.parametrize(
    "days",
    [["2026-10-14", "2026-10-16"], ["2026-10-17", "2026-10-18"], ["2026-10-15", "2026-10-14"]],
)
def test_scope_cannot_be_widened_or_reversed(tmp_path, days):
    _, config = setup(tmp_path)
    with pytest.raises(ValidationError):
        plan_for(config, first_window=days)


def test_changed_saved_plan_or_owner_requires_explicit_migration(tmp_path):
    service, config = setup(tmp_path)
    p = plan_for(config)
    raw = service.path.read_bytes()
    service.migrate_empty(p.admission_reader_sha256, sha(raw), "replan", plan=p)
    raw = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_REPLAN_REQUIRED"):
        service.migrate_empty(digest("yet-another-owner"), sha(raw), "cannot lose plan binding")
    state = json.loads(raw)
    state["calendar_plan"]["first_window"] = ["2026-10-15", "2026-10-16"]
    service.path.write_text(json.dumps(state))
    with pytest.raises(DomainError, match="PAPER_CALENDAR_PLAN_CHANGED"):
        service.status()


def test_cannot_first_open_on_second_day_or_outside_plan(tmp_path):
    from .fixtures import decision

    service, config = setup(tmp_path)
    p = plan_for(config)
    raw = service.path.read_bytes()
    service.migrate_empty(p.admission_reader_sha256, sha(raw), "replan", plan=p)
    _, config = service._load()
    for day in ("2026-10-13", "2026-10-15", "2026-10-16"):
        d = decision(config, 0, (100, 100))
        d.update(evidence_kind="calendar_fixture", clock_basis="fixture_clock", trading_date=day)
        with pytest.raises(DomainError, match="PAPER_REPLAN_DAY"):
            service.prepare_fixture(d)
    assert service.status()["days"] == {}


def test_cli_replan_checks_actual_owner_scope_before_changing_state(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from ashare_quant.paper.online import OnlineService
    from ashare_quant.paper.online_adapter import OwnerReader

    ad_source = os.environ.get("ASHARE_CALENDAR_AD_SOURCE")
    ad_python = os.environ.get("ASHARE_CALENDAR_AD_PYTHON")
    if not ad_source or not ad_python:
        pytest.skip("Explicit pinned AD source required")
    reader = OwnerReader(ad_python, Path(ad_source), tmp_path / "daily", tmp_path / "phases")
    owned = reader.reviewed_plan()
    _, base = setup(tmp_path / "template")
    config = base.model_copy(
        update={"universe": tuple(owned["scope"]), "data_mode": "calendar_observed"}
    )
    service = OnlineService(tmp_path / "control")
    service.create(config)
    raw = service.path.read_bytes()
    plan = plan_for(config, **owned)
    path = tmp_path / "plan.json"
    wrong = plan.model_dump(mode="json")
    wrong["calendar_source_sha256"] = digest("wrong-calendar")
    path.write_text(json.dumps(wrong))
    args = [
        sys.executable,
        "-B",
        "-m",
        "ashare_quant.paper.online_cli",
        "--root",
        str(service.root),
        "--ad-python",
        ad_python,
        "--ad-source",
        ad_source,
        "--ad-store",
        str(tmp_path / "daily"),
        "--phase-store",
        str(tmp_path / "phases"),
        "migrate-empty",
        "--state-sha",
        sha(raw),
        "--reason",
        "isolated CLI replan test",
        "--plan",
        str(path),
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    assert (
        result.returncode == 2
        and json.loads(result.stdout)["error"]["code"] == "PAPER_REPLAN_OWNER_SCOPE"
    )
    assert service.path.read_bytes() == raw
    path.write_text(plan.model_dump_json())
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr + result.stdout
    assert service.status()["calendar_plan"] == plan.model_dump(mode="json")
    assert (service.root / "migrations" / (sha(raw) + ".json")).read_bytes() == raw
    assert not service.status()["days"] and not list(service.root.rglob("*.chk"))


@pytest.mark.parametrize("damage", ["missing", "wrong_baseline"])
def test_observed_plan_requires_its_exact_action_baseline_before_any_native(tmp_path, damage):
    from ashare_quant.paper.online_contracts import OnlineDecision

    from .fixtures import decision

    service, config = setup(tmp_path)
    p = plan_for(config)
    raw = service.path.read_bytes()
    service.migrate_empty(p.admission_reader_sha256, sha(raw), "replan", plan=p)
    # The negative proof is checked before the real-clock/execution boundary.
    state = json.loads(service.path.read_bytes())
    state["config"]["data_mode"] = "calendar_observed"
    state["identity"] = digest({"config": state["config"], "engine": state["engine_pin"]})
    service.path.write_text(json.dumps(state))
    _, config = service._load()
    d = decision(config, 0, (100, 100))
    d.update(
        trading_date="2026-10-14", evidence_kind="live_local_capture", clock_basis="wall_clock"
    )
    if damage == "wrong_baseline":
        d["strategy_receipt"] = {
            "action_adjudications": {
                s: {
                    "action_scope": {
                        "price_share_code_review": {"baseline_sha256": digest("wrong")}
                    }
                }
                for s in config.universe
            }
        }
    with pytest.raises(DomainError, match="PAPER_REPLAN_ACTION_BASELINE"):
        service._prepare(OnlineDecision.model_validate(d))
    assert service.status()["days"] == {} and not list(tmp_path.rglob("*.chk"))


def test_observed_v4_owner_cannot_bypass_explicit_plan(tmp_path):
    from ashare_quant.paper.online_adapter import decision_from_owner

    _, config = setup(tmp_path)
    config = config.model_copy(update={"data_mode": "calendar_observed"})

    class Reader:
        def read(self, config, kind, capture):
            return {"schema": "ad-calendar-phase-observation.v4"} if kind == "admission" else {}

    with pytest.raises(DomainError, match="PAPER_EXPLICIT_REPLAN_REQUIRED"):
        decision_from_owner(config, {}, None, Reader(), digest("daily"), digest("admission"))
