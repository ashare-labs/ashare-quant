"""Synthetic account-origin boundaries; no claim of real market-source acceptance."""

import copy
from decimal import Decimal

import pytest

from ashare_quant.portfolio.audit import audit
from ashare_quant.portfolio.corporate_export import attach
from ashare_quant.portfolio.inputs import digest
from tests.portfolio.test_corporate_actions import KEY, SID, fixture, load, native


def prior_record_fixture(kind="ex_date"):
    manifest, document = fixture()
    schedule = document.pop("corporate_schedule")
    document.pop("corporate_account_model")
    document.pop("corporate_source_replacement")
    start = "2020-02-06" if kind == "delayed" else "2020-02-05"
    manifest["windows"][0]["start"] = start
    schedule["coverage"]["start"] = start
    if kind != "ex_date":
        schedule["rows"][0].update(
            payment_date="2020-02-07", share_credit_date="2020-02-07",
            new_shares_tradable_date="2020-02-10",
        )
    if kind == "before_warmup":
        schedule["rows"][0].update(record_date="2010-01-04", ex_date="2010-01-05")
        assert schedule["rows"][0]["ex_date"] < document["calendar"][0]
    original = copy.deepcopy(schedule)
    document = attach(document, schedule, accept_date_model=True)
    assert document["corporate_schedule"] == schedule == original
    return manifest, document


@pytest.mark.parametrize("kind", ["ex_date", "delayed", "before_warmup"])
def test_empty_start_retains_source_schedule_and_has_no_inherited_rights(tmp_path, kind):
    manifest, doc = prior_record_fixture(kind)
    schedule = copy.deepcopy(doc["corporate_schedule"])
    source_sha = digest(schedule)
    data = load(tmp_path, doc, manifest)
    assert data.preflight()["ready"]
    assert data.corporate_actions == schedule["rows"]
    result = native(tmp_path, manifest=manifest, doc=doc, targets={"12": 1000, "8": 1000})
    assert [(t["date"], t["side"], t["quantity"]) for t in result["trades"]] == [
        (manifest["windows"][0]["start"], "BUY", 1000)
    ]
    assert result["corporate_entitlements"][SID] == {}  # No invented historical record stage.
    assert result["corporate_schedule_sha256"] == source_sha
    assert doc["corporate_schedule"] == schedule
    assert digest(data.document["corporate_schedule"]) == source_sha
    assert data.document["action_coverage"]["coverage"]["rows"][0]["events"] == 1
    for account in result["accounts"]:
        assert Decimal(account["cash"]) == 91995  # 100000 - 1000 * 8 - 5 commission.
        assert Decimal(account["equity"]) == 99995
        position = account["positions"][SID]
        assert position["quantity"] == 1000
        assert position["restricted_shares"] == position["pending_shares"] == 0
        assert Decimal(position["dividend_receivable"]) == 0
        assert Decimal(position["pending_share_value"]) == 0
    report = audit(result, data, manifest)
    assert report["passed"] and Decimal(report["gross_dividend_entitlement"]) == 0


@pytest.mark.parametrize("kind", ["ex_date", "delayed", "before_warmup"])
def test_seed_cannot_substitute_for_prior_record_basis(tmp_path, kind):
    manifest, doc = prior_record_fixture(kind)
    manifest["execution"].update(
        initial_positions={SID: 1000}, initial_position_model="research_seed_previous_close"
    )
    expected = "CORPORATE_RECORD_BASIS_OUTSIDE_RUN:" + SID + ":" + doc[
        "corporate_schedule"]["rows"][0]["record_date"]
    with pytest.raises(ValueError) as error:
        load(tmp_path, doc, manifest)
    assert str(error.value).startswith(expected + ":")
    assert "record holdings" in str(error.value)
    refused = native(tmp_path, manifest=manifest, doc=doc, expect="refused")
    assert expected in refused
    assert not (tmp_path / "result" / "native.chk").exists()
    assert not list((tmp_path / "result").iterdir())


@pytest.mark.parametrize("initial", [
    None, [], 0, False, "1000", {SID: True}, {SID: 0}, {SID: -1},
    {SID: 1.5}, {SID: "1000"}, {SID: 2**53 + 1}, {"600036.XSHG": 1000},
])
def test_invalid_seed_is_rejected_before_prior_record_policy(tmp_path, initial):
    manifest, doc = prior_record_fixture()
    manifest["execution"].update(
        initial_positions=initial, initial_position_model="research_seed_previous_close"
    )
    with pytest.raises(ValueError, match="CORPORATE_INITIAL_HOLDING_MODEL_REQUIRED"):
        load(tmp_path, doc, manifest)


@pytest.mark.parametrize("model", [None, "inherit_unknown_ledger"])
def test_seed_needs_the_explicit_model_even_when_record_is_prior(tmp_path, model):
    manifest, doc = prior_record_fixture()
    manifest["execution"].update(initial_positions={SID: 1000}, initial_position_model=model)
    with pytest.raises(ValueError, match="CORPORATE_INITIAL_HOLDING_MODEL_REQUIRED"):
        load(tmp_path, doc, manifest)


def test_unrelated_seed_does_not_create_rights_for_the_unseeded_security(tmp_path):
    manifest, doc = prior_record_fixture()
    other = "600036.XSHG"
    manifest["securities"].append(other)
    doc["securities"].append(other)
    doc["daily"] += [dict(row, security=other) for row in doc["daily"]]
    doc["corporate_schedule"]["coverage"]["rows"].append(dict(
        security=other, provider_tables_captured=True, economic_schedule_fields_complete=False,
        events=0, source_gaps=[], event_field_gaps=[], identity_boundaries=[],
        interval_state="empty_source_scoped_interval",
    ))
    manifest["execution"].update(
        initial_positions={other: 1000}, initial_position_model="research_seed_previous_close"
    )
    assert load(tmp_path, doc, manifest).preflight()["ready"]


def test_seed_does_not_need_basis_for_a_fully_completed_prior_event(tmp_path):
    manifest, doc = fixture()
    manifest["windows"][0]["start"] = "2020-02-10"
    manifest["execution"].update(
        initial_positions={SID: 1000}, initial_position_model="research_seed_previous_close"
    )
    data = load(tmp_path, doc, manifest)
    assert data.preflight()["ready"] and data.corporate_actions == []
    assert len(data.document["corporate_schedule"]["rows"]) == 1


def test_seed_registered_inside_the_run_still_receives_cash_and_shares(tmp_path):
    manifest, doc = fixture()
    manifest["execution"].update(
        initial_positions={SID: 1000}, initial_position_model="research_seed_previous_close"
    )
    result = native(tmp_path, manifest=manifest, doc=doc)
    entry = result["corporate_entitlements"][SID][KEY]
    assert entry["basis"] == 1000 and entry["shares"] == 400 and Decimal(entry["cash"]) == 260
    assert entry["stages"][KEY + ":record"] == "2020-02-04"
    assert not result["trades"]
    assert Decimal(result["final_account"]["cash"]) == 100260
    assert result["final_account"]["positions"][SID]["quantity"] == 1400
    report = audit(result, load(tmp_path, doc, manifest), manifest)
    assert report["passed"] and Decimal(report["gross_dividend_entitlement"]) == 260


@pytest.mark.parametrize("fault,expected", [
    ("source_issue", "SOURCE_ISSUES"), ("coverage_gap", "COVERAGE_GAP"),
    ("missing_payment", "DATE_REQUIRED"), ("conflict", "EVENT_CONFLICT"),
    ("chronology", "INVALID_CHRONOLOGY"), ("payment_non_session", "STAGE_NOT_SESSION"),
])
def test_prior_record_does_not_hide_invalid_source_evidence(tmp_path, fault, expected):
    manifest, doc = prior_record_fixture()
    schedule, event = doc["corporate_schedule"], doc["corporate_schedule"]["rows"][0]
    if fault == "source_issue":
        schedule["issues"] = [{"field": "payment_date", "reason": "conflict"}]
    elif fault == "coverage_gap":
        schedule["coverage"]["rows"][0]["source_gaps"] = [{"reason": "parse_failed"}]
    elif fault == "missing_payment":
        event["payment_date"] = None
    elif fault == "conflict":
        event["conflicts"] = ["payment_date"]
    elif fault == "chronology":
        event["ex_date"] = event["record_date"]
    elif fault == "payment_non_session":
        event["payment_date"] = "2020-02-08"
    with pytest.raises(ValueError, match=expected):
        load(tmp_path, doc, manifest)


def test_audit_rejects_invented_prior_entitlement_and_its_cash(tmp_path):
    manifest, doc = prior_record_fixture()
    result = native(tmp_path, manifest=manifest, doc=doc, targets={"12": 1000, "8": 1000})
    data = load(tmp_path, doc, manifest)
    invented = copy.deepcopy(result)
    event = doc["corporate_schedule"]["rows"][0]
    invented["corporate_entitlements"][SID][KEY] = {
        "event": event, "source_sha256": digest(event), "basis": 1000, "shares": 400,
        "cash": "260", "pending_cost": "0",
        "stages": {KEY + ":record": event["record_date"], KEY + ":cash": event["payment_date"]},
    }
    report = audit(invented, data, manifest)
    assert not report["passed"]
    assert "CORPORATE_JOURNAL_COVERAGE" in {e["code"] for e in report["errors"]}
    invented_cash = copy.deepcopy(result)
    for account in invented_cash["accounts"]:
        account["cash"] = str(Decimal(account["cash"]) + 260)
        account["equity"] = str(Decimal(account["equity"]) + 260)
    report = audit(invented_cash, data, manifest)
    assert not report["passed"]
    assert "CASH_FROM_FILLS_AND_ACTIONS" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("fault", ["native_submit:2", "after_replace:3"])
def test_empty_prior_record_restart_matches_baseline_without_gaining_entitlement(tmp_path, fault):
    manifest, doc = prior_record_fixture()
    options = dict(manifest=manifest, doc=doc, targets={"12": 1000, "8": 1000})
    baseline = native(tmp_path, root=tmp_path / "baseline", invocation="baseline", **options)
    crash = tmp_path / "crash"
    native(tmp_path, root=crash, invocation="fault", fault=fault, expect="killed", **options)
    recovered = native(tmp_path, root=crash, invocation="recover", **options)
    assert recovered == baseline and recovered["corporate_entitlements"][SID] == {}
    checkpoint = (crash / "native.chk").read_bytes()
    assert native(tmp_path, root=crash, invocation="repeat", **options) == baseline
    assert (crash / "native.chk").read_bytes() == checkpoint
