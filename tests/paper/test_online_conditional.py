"""Independent staged fixtures; unknown pre-open state cannot authorize a fill."""

import copy
import json
from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.online import OnlineService
from ashare_quant.paper.online_contracts import CONDITIONAL_MODEL, OnlineSession
from ashare_quant.storage.artifacts import digest
from tests.paper.fixtures import decision
from tests.support.paper_online import MODE, closing, opening, setup


def conditional(tmp_path):
    _, old = setup(tmp_path / "legacy")
    config = OnlineSession.model_validate(
        {**old.model_dump(mode="json"), "model": CONDITIONAL_MODEL}
    )
    service = OnlineService(tmp_path / "conditional")
    service.create(config)
    return service, config


def prepare(service, config, index=0, targets=(100, 100), parent=None):
    d = {**decision(config, index, targets, parent), **MODE}
    for row in d["rows"]:
        row["suspended"] = None
    return service.prepare_fixture(d)


def observed_open(row):
    value = opening(row)
    value["execution_admission"] = {
        "schema_version": "ad-calendar-phase-observation.v5",
        "capture_id": digest("explicit-fixture-opening-refresh"),
        "observed_at": value["received_at"],
        "rows": [
            {
                **{
                    k: r[k]
                    for k in (
                        "security",
                        "eligible",
                        "is_st",
                        "lower_limit",
                        "upper_limit",
                        "rule_version",
                    )
                },
                "suspended": False,
                "reference_close": r["previous_close"],
                "action_baseline_sha256": digest("fixture-reviewed-actions"),
                "action_review_sha256": digest(["fixture-new-action-receipt", r["security"]]),
            }
            for r in row["decision"]["rows"]
        ],
    }
    return value


def test_conditional_prepare_has_no_orders_and_needs_current_execution_proof(tmp_path):
    service, config = conditional(tmp_path)
    row = prepare(service, config)
    assert all(r["suspended"] is None for r in row["decision"]["rows"])
    assert service.status()["latest_account"] is None
    assert not list(service.root.rglob("*.chk"))
    before = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_OPENING_ADMISSION_REQUIRED"):
        service.deliver_fixture("opening", opening(row))
    assert service.path.read_bytes() == before
    assert not list(service.root.rglob("*request.json"))


@pytest.mark.parametrize(
    "damage,error",
    [
        ("halt", "PAPER_OPENING_TRADING_STATE_BLOCKED"),
        ("st", "PAPER_OPENING_TRADING_STATE_BLOCKED"),
        ("identity", "PAPER_SECURITY_SET"),
        ("ref", "PAPER_OPENING_PARAMETERS_CHANGED"),
        ("limit", "PAPER_OPENING_PARAMETERS_CHANGED"),
        ("rule", "PAPER_OPENING_PARAMETERS_CHANGED"),
        ("clock", "PAPER_OPENING_ADMISSION_CLOCK"),
        ("stale_quote", "PAPER_OPENING_NOT_OBSERVED"),
    ],
)
def test_opening_rechecks_refuse_before_any_native_order(tmp_path, damage, error):
    service, config = conditional(tmp_path)
    row = prepare(service, config)
    value = observed_open(row)
    fact = value["execution_admission"]["rows"][0]
    if damage == "halt":
        fact["suspended"] = True
    elif damage == "st":
        fact["is_st"] = True
    elif damage == "identity":
        fact["security"] = "600036.XSHG"
    elif damage == "ref":
        fact["reference_close"] = "9.99"
    elif damage == "limit":
        fact["upper_limit"] = "12"
    elif damage == "rule":
        fact["rule_version"] = "changed"
    elif damage == "clock":
        value["execution_admission"]["observed_at"] = row["day"] + "T08:55:00+08:00"
    else:
        value["rows"][0]["source_as_of"] = row["day"] + "T08:55:00+08:00"
    before = service.path.read_bytes()
    with pytest.raises(DomainError, match=error):
        service.deliver_fixture("opening", value)
    assert service.path.read_bytes() == before
    assert not list(service.root.rglob("*request.json"))


def test_two_day_native_continuation_restart_duplicate_and_atomic_recovery(tmp_path):
    service, config = conditional(tmp_path)
    parent = None
    reports = []
    for index, targets in enumerate(((100, 100), (0, 100))):
        row = prepare(service, config, index, targets, parent)
        value = observed_open(row)
        service.deliver_fixture("opening", value)
        assert (
            service.deliver_fixture("opening", copy.deepcopy(value))["intent_id"]
            == row["intent_id"]
        )
        if index == 0:
            with pytest.raises(DomainError, match="exited -9"):
                service.advance(row["day"], "opening", fault="inside_native_trade")
            service = OnlineService(service.root)
            service.resume("controlled fixture crash with unchanged conditional opening proof")
        opened = service.advance(row["day"], "opening")
        assert service.advance(row["day"], "opening") == opened
        request = json.loads((service.root / row["day"] / "opening-request.json").read_text())
        assert request["decision"]["rows"][0]["suspended"] is None
        assert request["opening"]["execution_admission"]["rows"][0]["suspended"] is False
        service = OnlineService(service.root)
        service.deliver_fixture("closing", closing(row))
        closed = service.advance(row["day"], "closing")
        assert closed["native"]["trades"] == opened["native"]["trades"]
        parent = closed["checkpoint_sha256"]
        reports.append(closed)
    assert len(reports[0]["native"]["trades"]) == 2
    assert len(reports[1]["native"]["trades"]) == 1
    assert reports[1]["native"]["before"]["positions"]["000001.XSHE"]["sellable"] == 100
    assert Decimal(reports[1]["native"]["account"]["total_cash"]) == Decimal("7984")
    assert reports[1]["native"]["settlement_count"] == 2
    assert service.status()["real_calendar_observation"] is False


def test_native_risk_remains_effective_after_conditional_admission(tmp_path):
    service, config = conditional(tmp_path)
    row = prepare(service, config, targets=(900, 900))
    service.deliver_fixture("opening", observed_open(row))
    with pytest.raises(DomainError, match="Native worker exited"):
        service.advance(row["day"], "opening")
    assert service.status()["head"] is None and service.status()["latest_account"] is None
    logs = list((service.root / row["day"]).glob("native-*.log"))
    assert "PAPER_CONCENTRATION_LIMIT" in logs[0].read_text()


def test_legacy_model_still_refuses_unknown_state_and_new_execution_proof(tmp_path):
    service, config = setup(tmp_path)
    with pytest.raises(DomainError, match="PAPER_TRADING_STATE_BLOCKED"):
        prepare(service, config)
    d = {**decision(config, 0, (100, 100)), **MODE}
    row = service.prepare_fixture(d)
    with pytest.raises(DomainError, match="PAPER_EXPLICIT_CONDITIONAL_MODEL_REQUIRED"):
        service.deliver_fixture("opening", observed_open(row))


def test_model_change_requires_explicit_empty_plan_and_preserves_old_bytes(tmp_path):
    from ashare_quant.storage.artifacts import sha
    from tests.paper.test_calendar_replan import plan_for

    service, config = setup(tmp_path)
    raw = service.path.read_bytes()
    plan = plan_for(config, model=CONDITIONAL_MODEL)
    result = service.migrate_empty(
        plan.admission_reader_sha256,
        sha(raw),
        "fixture-only explicit new temporal contract",
        plan=plan,
    )
    assert result["config"]["model"] == CONDITIONAL_MODEL
    assert result["days"] == {} and result["head"] is None
    assert (service.root / "migrations" / (sha(raw) + ".json")).read_bytes() == raw


def test_owner_opening_adapter_requires_v5_and_carries_refreshed_proof(tmp_path):
    from ashare_quant.paper.online_adapter import execution_from_owner

    service, config = conditional(tmp_path)
    row = prepare(service, config)
    value = observed_open(row)
    facts = []
    for quote, fact in zip(value["rows"], value["execution_admission"]["rows"], strict=True):
        facts.append(
            {
                **quote,
                **fact,
                "action_scope": {
                    "price_share_code_review": {
                        "baseline_sha256": fact["action_baseline_sha256"],
                        "source_sha256": fact["action_review_sha256"],
                    }
                },
            }
        )
    packet = dict(
        schema="ad-calendar-phase-observation.v5",
        kind="opening",
        day=row["day"],
        completed_at=value["received_at"],
        assessment=dict(opening_ready=True, execution_ready=True, blockers=[], rows=facts),
    )

    class Reader:
        def read(self, *args):
            return packet

    result = execution_from_owner(config, row, Reader(), "opening", digest("source"))
    assert result.execution_admission.rows[0].suspended is False
    assert result.execution_admission.rows[0].action_baseline_sha256 == digest(
        "fixture-reviewed-actions"
    )
    packet["assessment"]["execution_ready"] = False
    with pytest.raises(DomainError, match="PAPER_OPENING_ADMISSION_REQUIRED"):
        execution_from_owner(config, row, Reader(), "opening", digest("source"))
    packet["schema"] = "ad-calendar-phase-observation.v4"
    with pytest.raises(DomainError, match="PAPER_EXPLICIT_CONDITIONAL_MODEL_REQUIRED"):
        execution_from_owner(config, row, Reader(), "opening", digest("source"))


def test_owner_preparation_uses_explicit_v5_readiness_without_inventing_normal_state(tmp_path):
    from pathlib import Path

    import ashare_quant.portfolio.signals as signals
    from ashare_quant.paper.online_adapter import UNIVERSE, decision_from_owner
    from ashare_quant.storage.artifacts import sha

    _, base = conditional(tmp_path)
    config = base.model_copy(
        update={"universe": UNIVERSE, "strategy_version": sha(Path(signals.__file__).read_bytes())}
    )
    prior, day, after = "2026-10-13", "2026-10-14", "2026-10-15"
    daily = dict(
        signal_day=prior,
        completed_at=prior + "T17:45:00+08:00",
        signal_ready=True,
        revisions=[],
        calendar=dict(
            next_open=day,
            open_days=[prior, day, after],
            source=dict(observed_at=prior + "T17:45:00+08:00"),
        ),
        securities={
            s: dict(
                rows=[{"close": "10"}] * 120, source=dict(observed_at=prior + "T17:45:00+08:00")
            )
            for s in UNIVERSE
        },
    )
    admission = dict(
        schema="ad-calendar-phase-observation.v5",
        kind="admission",
        day=day,
        completed_at=day + "T08:55:00+08:00",
        assessment=dict(
            preparation_ready=True,
            execution_permission=False,
            blockers=[],
            rows=[
                dict(
                    security=s,
                    eligible=True,
                    suspended=None,
                    is_st=False,
                    reference_close="10",
                    lower_limit="9",
                    upper_limit="11",
                    rule_version="FIXTURE",
                    actions_known_absent=True,
                )
                for s in UNIVERSE
            ],
        ),
    )

    class Reader:
        def read(self, config, kind, capture):
            return daily if kind == "daily" else admission

    def build():
        return decision_from_owner(
            config, {"head": None}, None, Reader(), digest("daily"), digest("admission")
        )

    result = build()
    assert all(r.suspended is None for r in result.rows)
    assert result.targets and result.strategy_receipt["scheduled"]
    admission["assessment"]["rows"][0]["reference_close"] = "9.99"
    with pytest.raises(DomainError, match="PAPER_REFERENCE_CLOSE_CHANGED"):
        build()
    admission["assessment"]["rows"][0]["reference_close"] = "10"
    admission["assessment"]["preparation_ready"] = False
    with pytest.raises(DomainError) as failure:
        build()
    assert failure.value.code == "PAPER_CURRENT_ADMISSION_BLOCKED"
    admission.update(schema="ad-calendar-phase-observation.v4")
    admission["assessment"]["execution_permission"] = True
    with pytest.raises(DomainError, match="PAPER_EXPLICIT_CONDITIONAL_MODEL_REQUIRED"):
        build()
