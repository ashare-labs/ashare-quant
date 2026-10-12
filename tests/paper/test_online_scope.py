import json

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest, sha
from ashare_quant.paper.online_adapter import scoped_action_effect
from tests.paper.test_online import prepare, setup


def test_explicit_empty_migration_preserves_old_identity(tmp_path):
    service, _ = setup(tmp_path)
    raw = service.path.read_bytes()
    old = json.loads(raw)
    result = service.migrate_empty(digest("reviewed-new-owner"), sha(raw), "review fixes R1-R3")
    assert result["identity"] != old["identity"]
    assert result["days"] == {} and result["head"] is None
    assert (service.root / "migrations" / (sha(raw) + ".json")).read_bytes() == raw
    assert result["events"][-1]["details"]["previous_identity"] == old["identity"]
    with pytest.raises(DomainError, match="PAPER_MIGRATION_STATE_CHANGED"):
        service.migrate_empty(digest("reviewed-new-owner"), sha(raw), "repeat stale request")


@pytest.mark.parametrize("kind", ["prepared", "orphan_checkpoint", "orphan_day"])
def test_migration_refuses_any_prior_native_or_intent_evidence(tmp_path, kind):
    service, config = setup(tmp_path)
    if kind == "prepared":
        prepare(service, config)
    elif kind == "orphan_checkpoint":
        (service.root / "native.chk").write_bytes(b"orphan")
    else:
        (service.root / "2026-10-13").mkdir()
    raw = service.path.read_bytes()
    with pytest.raises(DomainError, match="PAPER_MIGRATION_REQUIRES_EMPTY_ACCOUNT"):
        service.migrate_empty(digest("new-owner"), sha(raw), "cannot clear a live account")
    assert service.path.read_bytes() == raw


def fact():
    return dict(
        actions_known_absent=False,
        action_scope=dict(
            from_day="2026-10-13",
            through_day="2026-10-14",
            price_share_code_review=dict(status="proven", source_sha256=digest("fixture-review")),
        ),
        cash_inventory=dict(
            cash_history_complete=True,
            unresolved=[],
            events=[
                dict(
                    type="cash_dividend",
                    record_date="2026-09-09",
                    ex_date="2026-09-10",
                    pay_date=None,
                )
            ],
        ),
    )


def test_zero_entitlement_is_explicit_and_never_means_no_actions():
    value = fact()
    effect, evidence = scoped_action_effect({"days": {}}, value, "2026-10-13")
    assert effect == "zero_entitlement" and evidence["genesis"] == "2026-10-13"
    assert value["actions_known_absent"] is False
    value["action_scope"]["price_share_code_review"] = None
    with pytest.raises(DomainError, match="PAPER_ACTION_SCOPE_REVIEW_MISSING"):
        scoped_action_effect({"days": {}}, value, "2026-10-13")


def test_nonzero_entitlement_or_current_price_effect_never_ignored():
    value = fact()
    with pytest.raises(DomainError, match="PAPER_ACTION_ENTITLEMENT_UNSUPPORTED"):
        scoped_action_effect({"days": {"2026-09-01": {}}}, value, "2026-10-13")
    value["cash_inventory"]["events"][0]["ex_date"] = "2026-10-13"
    with pytest.raises(DomainError, match="PAPER_ACTION_ENTITLEMENT_UNSUPPORTED"):
        scoped_action_effect({"days": {}}, value, "2026-10-13")
