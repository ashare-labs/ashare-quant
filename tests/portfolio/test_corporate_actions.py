"""Synthetic date-stage rules, each with an independent economic assertion."""

import copy
import json
import os
import runpy
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import ashare_quant
from ashare_quant.portfolio.corporate import MODEL, STAGES
from ashare_quant.portfolio.corporate_export import attach
from ashare_quant.portfolio.inputs import Inputs

REPO = Path(__file__).resolve().parents[2]
SID = "600000.XSHG"
KEY = SID + ":distribution:synthetic-plan-one"


def fixture():
    manifest = json.loads((REPO / "examples/portfolio/manifest.json").read_text())
    manifest["securities"] = [SID]
    manifest["windows"] = [{"id": "test", "start": "2020-02-03", "end": "2020-02-10"}]
    config = {"id": "daily-equal-weight", "purpose": "Synthetic fixed-target date-stage rule"}
    manifest["strategies"] = [config]
    manifest["execution"]["initial_cash"] = "100000"
    document = runpy.run_path(str(REPO / "scripts/verify_portfolio.py"))["generate"](
        manifest, True
    )
    for row in document["daily"]:
        price = "10" if row["date"] < "2020-02-04" else "12" if row["date"] == "2020-02-04" else "8"
        row.update(
            open=price,
            high=price,
            low=price,
            close=price,
            suspended=False,
            upper_limit="30",
            lower_limit="1",
        )
    event = dict(
        security=SID,
        type="distribution",
        event_id=KEY,
        entitlement_key=KEY,
        settlement_keys={s: KEY + ":" + s for s in STAGES},
        revision_observation_ids=["synthetic-observation-1"],
        identity_issues=[],
        conflicts=[],
        record_date="2020-02-04",
        ex_date="2020-02-05",
        payment_date="2020-02-05",
        share_credit_date="2020-02-05",
        new_shares_tradable_date="2020-02-06",
        cash_per_share="0.26",
        bonus_share_ratio="0",
        capitalization_share_ratio="0.4",
    )
    schedule = dict(
        schema="ad.aq-corporate-actions.v1",
        binding_sha256="a" * 64,
        event_identity_version=2,
        source_scoped_coverage_accepted=True,
        tax_model="gross_cash_no_tax",
        ready_for_declared_date_model=True,
        issues=[],
        historical_pit=False,
        strategy_visible=False,
        execution_permission=False,
        economic_result_certified=False,
        rows=[event],
        coverage=dict(
            binding_sha256="a" * 64,
            start="2020-02-03",
            end="2020-02-10",
            rows=[
                dict(
                    security=SID,
                    provider_tables_captured=True,
                    economic_schedule_fields_complete=True,
                    events=1,
                    source_gaps=[],
                    event_field_gaps=[],
                    identity_boundaries=[],
                    interval_state="events_observed",
                )
            ],
        ),
    )
    document = attach(document, schedule, accept_date_model=True)
    return manifest, document


def load(tmp_path, doc, manifest):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(doc))
    return Inputs(path, manifest, manifest["windows"][0])


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("share_credit_date", None, "DATE_REQUIRED"),
        ("new_shares_tradable_date", "2020-02-04", "CHRONOLOGY"),
        ("cash_per_share", "NaN", "INVALID_NUMBER"),
        ("cash_per_share", 0.26, "DECIMAL_STRING"),
        ("cash_per_share", "-1", "INVALID_NUMBER"),
        ("type", "rights_issue", "EVENT_TYPE_UNSUPPORTED"),
        ("type", "reorganization", "EVENT_TYPE_UNSUPPORTED"),
        ("type", "security_conversion", "EVENT_TYPE_UNSUPPORTED"),
        ("conflicts", ["payment_date"], "EVENT_CONFLICT"),
        ("revision_observation_ids", [], "REVISION_EVIDENCE_REQUIRED"),
        ("record_date", "2020-02-03", "NONADJACENT"),
        ("payment_date", "2020-02-08", "STAGE_NOT_SESSION"),
    ],
)
def test_bad_event_blocked(tmp_path, field, value, error):
    manifest, doc = fixture()
    doc["corporate_schedule"]["rows"][0][field] = value
    with pytest.raises(ValueError, match=error):
        load(tmp_path, doc, manifest)


def test_coverage_conflicts_and_missing_basis_cannot_disappear_by_selecting_window(tmp_path):
    manifest, doc = fixture()
    manifest["windows"][0]["start"] = "2020-02-05"
    with pytest.raises(ValueError, match="RECORD_BASIS_OUTSIDE_RUN"):
        load(tmp_path, doc, manifest)
    doc["corporate_schedule"]["issues"] = [{"field": "payment_date", "reason": "conflict"}]
    with pytest.raises(ValueError, match="SOURCE_ISSUES"):
        load(tmp_path, doc, manifest)


def test_source_unknown_is_not_empty_history(tmp_path):
    manifest, doc = fixture()
    doc["corporate_schedule"]["coverage"]["rows"][0]["source_gaps"] = [{"reason": "parse_failed"}]
    with pytest.raises(ValueError, match="COVERAGE_GAP"):
        load(tmp_path, doc, manifest)


def test_explicit_source_scoped_empty_interval_is_not_unknown_history(tmp_path):
    manifest, doc = fixture()
    schedule = doc["corporate_schedule"]
    schedule["rows"] = []
    row = schedule["coverage"]["rows"][0]
    row.update(
        events=0,
        interval_state="empty_source_scoped_interval",
        economic_schedule_fields_complete=False,
    )
    assert load(tmp_path, doc, manifest).corporate_actions == []
    row["interval_state"] = "source_unresolved"
    with pytest.raises(ValueError, match="COVERAGE_GAP"):
        load(tmp_path, doc, manifest)


def test_duplicates_dedup_and_changed_revision_refused(tmp_path):
    manifest, doc = fixture()
    row = copy.deepcopy(doc["corporate_schedule"]["rows"][0])
    doc["corporate_schedule"]["rows"].append(row)
    assert len(load(tmp_path, doc, manifest).corporate_actions) == 1
    row["cash_per_share"] = "0.27"
    with pytest.raises(ValueError, match="REVISION_RECONCILIATION_REQUIRED"):
        load(tmp_path, doc, manifest)


def test_document_resolved_precision_is_retained_but_unresolved_conflict_blocks(tmp_path):
    manifest, doc = fixture()
    row = doc["corporate_schedule"]["rows"][0]
    resolved = dict(
        status="document_resolved_conflict",
        value="0.26000001",
        resolution="Synthetic cited high-precision amount",
        observations=[{"value": "0.26"}, {"value": "0.26000001"}],
    )
    row["cash_per_share"] = resolved["value"]
    row["field_lineage"] = {"cash_per_share": resolved}
    row["conflicts"] = [{"field": "cash_per_share", **resolved}]
    assert load(tmp_path, doc, manifest).corporate_actions[0]["cash_per_share"] == "0.26000001"
    row["conflicts"][0]["status"] = "conflict"
    with pytest.raises(ValueError, match="CORPORATE_EVENT_CONFLICT"):
        load(tmp_path, doc, manifest)


def test_source_row_deletion_and_unaccepted_model_refused(tmp_path):
    manifest, doc = fixture()
    doc["corporate_schedule"]["rows"] = []
    with pytest.raises(ValueError, match="EVENT_COUNT"):
        load(tmp_path, doc, manifest)
    doc["corporate_account_model"]["cash_availability"] = "unknown"
    with pytest.raises(ValueError, match="DATE_MODEL_NOT_ACCEPTED"):
        load(tmp_path, doc, manifest)


def native(
    tmp_path, *, doc=None, targets=None, fault="none", root=None, invocation="run", expect=0
):
    manifest, source = fixture()
    doc = source if doc is None else doc
    question = tmp_path / "question"
    directory = question / "daily-equal-weight"
    directory.mkdir(parents=True, exist_ok=True)
    (question / "manifest.json").write_text(json.dumps(manifest))
    (directory / "config.json").write_text(json.dumps(manifest["strategies"][0]))
    # Closed, synthetic rule capsule. Only previous-session price enters the rule.
    targets = targets or {"10": 1000, "12": 1400, "8": 1400}
    (directory / "strategy.py").write_text(
        "def on_decision(history, prior_equity):\n"
        f"    return {{'targets': {{{SID!r}: {targets!r}[history[{SID!r}][-1]]}}}}\n"
    )
    path = tmp_path / "input.json"
    path.write_text(json.dumps(doc))
    root = root or tmp_path / "result"
    command = [
        sys.executable,
        "-B",
        "-m",
        "ashare_quant.portfolio",
        "--manifest",
        str(question / "manifest.json"),
        "--input",
        str(path),
        "--strategy",
        "daily-equal-weight",
        "--window",
        "test",
        "--output",
        str(root),
        "--fault",
        fault,
        "--invocation",
        invocation,
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(ashare_quant.__file__).resolve().parents[1]),
            PYTHONDONTWRITEBYTECODE="1",
        ),
    )
    (tmp_path / (invocation + ".log")).write_text(proc.stdout + proc.stderr)
    if expect == "killed":
        assert proc.returncode in {-9, 247} and not (root / "result.json").exists(), proc.stderr[
            -4000:
        ]
        return
    if expect == "refused":
        assert proc.returncode != 0
        return proc.stdout + proc.stderr
    assert proc.returncode == expect, (proc.stdout + proc.stderr)[-5000:]
    assert json.loads((root / "audit.json").read_text())["passed"]
    return json.loads((root / "result.json").read_text())


def day(result, date):
    return next(a for a in result["accounts"] if a["date"] == date)


def test_native_separate_credit_tradability_and_no_duplicate_quantity(tmp_path):
    result = native(tmp_path)
    ex, unlock = day(result, "2020-02-05"), day(result, "2020-02-06")
    assert Decimal(ex["cash"]) == Decimal("90255")  # 100000 - 1000*10 - 5 + 260
    assert Decimal(ex["equity"]) == Decimal("101455")  # cash + 1400*8
    assert ex["positions"][SID]["quantity"] == unlock["positions"][SID]["quantity"] == 1400
    assert ex["positions"][SID]["sellable"] == 1000
    assert ex["positions"][SID]["restricted_shares"] == 400
    assert unlock["positions"][SID]["sellable"] == 1400
    assert unlock["positions"][SID]["restricted_shares"] == 0
    assert len(result["trades"]) == 1
    assert result["corporate_account_model"] == MODEL


def test_native_old_shares_sold_on_credit_new_shares_only_after_unlock(tmp_path):
    result = native(tmp_path, targets={"10": 1000, "12": 400, "8": 0})
    assert [(t["date"], t["side"], t["quantity"]) for t in result["trades"]] == [
        ("2020-02-03", "BUY", 1000),
        ("2020-02-05", "SELL", 1000),
        ("2020-02-06", "SELL", 400),
    ]
    assert day(result, "2020-02-05")["positions"][SID]["sellable"] == 0
    assert Decimal(result["final_account"]["cash"]) == Decimal("101433.8")


def test_native_delayed_credit_and_payment_survive_zero_old_holding(tmp_path):
    _, doc = fixture()
    event = doc["corporate_schedule"]["rows"][0]
    event.update(
        payment_date="2020-02-07",
        share_credit_date="2020-02-07",
        new_shares_tradable_date="2020-02-10",
    )
    result = native(tmp_path, doc=doc, targets={"10": 1000, "12": 0, "8": 0})
    ex = day(result, "2020-02-05")
    assert ex["positions"][SID]["quantity"] == 0
    assert ex["positions"][SID]["pending_shares"] == 400
    assert Decimal(ex["positions"][SID]["pending_share_value"]) == 3200
    assert Decimal(ex["positions"][SID]["dividend_receivable"]) == 260
    assert day(result, "2020-02-07")["positions"][SID]["restricted_shares"] == 400
    assert not result["final_account"]["positions"]
    assert Decimal(result["final_account"]["cash"]) == Decimal("101433.8")
    pnl = {r["date"]: Decimal(r["pnl"]) for r in result["corporate_daily_pnl"]}
    assert pnl["2020-02-05"] == Decimal("-553")
    assert pnl["2020-02-07"] == 0  # Credit transfers pending equity into holdings, not profit.


def test_pending_share_price_changes_belong_to_each_day_native_profit(tmp_path):
    _, doc = fixture()
    doc["corporate_schedule"]["rows"][0].update(
        payment_date="2020-02-07",
        share_credit_date="2020-02-07",
        new_shares_tradable_date="2020-02-10",
    )
    for row in doc["daily"]:
        if row["date"] == "2020-02-06":
            row.update(open="9", close="9", high="9", low="9")
        elif "2020-02-07" <= row["date"] <= "2020-02-10":
            row.update(open="11", close="11", high="11", low="11")
    result = native(tmp_path, doc=doc, targets={"10": 1000, "12": 0, "8": 0, "9": 0, "11": 0})
    pnl = {r["date"]: Decimal(r["pnl"]) for r in result["corporate_daily_pnl"]}
    assert pnl["2020-02-06"] == 400 and pnl["2020-02-07"] == 800
    assert pnl["2020-02-10"] == Decimal("-9.4")


def test_native_fractional_entitlement_blocks_without_rounding(tmp_path):
    _, doc = fixture()
    doc["corporate_schedule"]["rows"][0]["capitalization_share_ratio"] = "0.0004"
    assert "CORPORATE_FRACTIONAL_ENTITLEMENT_UNSUPPORTED" in native(
        tmp_path, doc=doc, expect="refused"
    )


def test_native_new_buys_have_t1_independent_of_corporate_restriction(tmp_path):
    result = native(tmp_path, targets={"10": 1000, "12": 1600, "8": 1600})
    ex = day(result, "2020-02-05")["positions"][SID]
    assert ex["quantity"] == 1600 and ex["restricted_shares"] == 400 and ex["sellable"] == 1000
    assert day(result, "2020-02-06")["positions"][SID]["sellable"] == 1600


def test_native_tiny_fraction_cannot_round_into_integer_entitlement(tmp_path):
    _, doc = fixture()
    event = doc["corporate_schedule"]["rows"][0]
    event.update(bonus_share_ratio="0.4", capitalization_share_ratio="0." + "0" * 60 + "4")
    assert "CORPORATE_FRACTIONAL_ENTITLEMENT_UNSUPPORTED" in native(
        tmp_path, doc=doc, expect="refused"
    )


def test_native_record_day_buy_gets_entitlement_despite_t1(tmp_path):
    _, doc = fixture()
    prior = next(r for r in doc["daily"] if r["date"] == "2020-01-31")
    prior.update(open="9", high="9", low="9", close="9")
    result = native(tmp_path, doc=doc, targets={"9": 0, "10": 1000, "12": 1400, "8": 1400})
    assert day(result, "2020-02-04")["positions"][SID]["sellable"] == 0
    assert result["corporate_entitlements"][SID][KEY]["basis"] == 1000
    assert Decimal(result["final_account"]["cash"]) == 88255


def test_native_post_record_purchase_has_no_old_share_entitlement(tmp_path):
    result = native(tmp_path, targets={"10": 0, "12": 1000, "8": 1000})
    ex = day(result, "2020-02-05")
    assert ex["positions"][SID]["quantity"] == 1000
    assert ex["positions"][SID]["sellable"] == 0
    assert Decimal(ex["cash"]) == 91995
    entry = result["corporate_entitlements"][SID][KEY]
    assert entry["basis"] == entry["shares"] == 0 and Decimal(entry["cash"]) == 0


def test_native_multiple_entitlements_keep_separate_record_bases_and_payment_dates(tmp_path):
    _, doc = fixture()
    one = doc["corporate_schedule"]["rows"][0]
    one["payment_date"] = "2020-02-07"
    two = copy.deepcopy(one)
    second_key = SID + ":distribution:synthetic-plan-two"
    two.update(
        event_id=second_key,
        entitlement_key=second_key,
        settlement_keys={s: second_key + ":" + s for s in STAGES},
        revision_observation_ids=["synthetic-observation-2"],
        record_date="2020-02-06",
        ex_date="2020-02-07",
        payment_date="2020-02-10",
        capitalization_share_ratio="0",
        share_credit_date=None,
        new_shares_tradable_date=None,
        cash_per_share="0.1",
    )
    doc["corporate_schedule"]["rows"].append(two)
    doc["corporate_schedule"]["coverage"]["rows"][0]["events"] = 2
    result = native(tmp_path, doc=doc)
    assert result["corporate_entitlements"][SID][KEY]["basis"] == 1000
    assert result["corporate_entitlements"][SID][second_key]["basis"] == 1400
    assert Decimal(day(result, "2020-02-07")["positions"][SID]["dividend_receivable"]) == 140
    assert Decimal(result["final_account"]["cash"]) == 90395


def overlap_fixture(stock="0", first_credit="2020-02-07"):
    """Two source plans, distinct records; first credit crosses the second ex date."""
    manifest, doc = fixture()
    schedule = doc.pop("corporate_schedule")
    doc.pop("corporate_account_model")
    doc.pop("corporate_source_replacement")
    one = schedule["rows"][0]
    one.update(
        payment_date="2020-02-10",
        share_credit_date=first_credit,
        new_shares_tradable_date="2020-02-10",
    )
    two = copy.deepcopy(one)
    key = SID + ":distribution:synthetic-plan-two"
    two.update(
        event_id=key,
        entitlement_key=key,
        settlement_keys={s: key + ":" + s for s in STAGES},
        revision_observation_ids=["synthetic-observation-2"],
        record_date="2020-02-06",
        ex_date="2020-02-07",
        payment_date="2020-02-07",
        cash_per_share="0.1",
        capitalization_share_ratio=stock,
        share_credit_date="2020-02-10" if Decimal(stock) else None,
        new_shares_tradable_date="2020-02-10" if Decimal(stock) else None,
    )
    schedule["rows"].append(two)
    schedule["coverage"]["rows"][0]["events"] = 2
    for row in doc["daily"]:
        price = {"2020-02-06": "9", "2020-02-07": "11", "2020-02-10": "13"}.get(row["date"])
        if price:
            row.update(open=price, high=price, low=price, close=price)
    return manifest, attach(doc, schedule, accept_date_model=True)


@pytest.mark.parametrize(
    "stock,first_credit", [("0", "2020-02-07"), ("0.2", "2020-02-07"), ("0.2", "2020-02-10")]
)
def test_overlapping_distributions_preserve_frozen_bases_cost_and_equity(
    tmp_path, stock, first_credit
):
    manifest, doc = overlap_fixture(stock, first_credit)
    assert load(tmp_path, doc, manifest).preflight()["ready"]
    credited = 1400 if first_credit == "2020-02-07" else 1000
    final_quantity = 1400 + int(1000 * Decimal(stock))
    result = native(
        tmp_path,
        doc=doc,
        targets={"10": 1000, "12": 1000, "8": 1000, "9": credited, "11": final_quantity},
    )
    entries = list(result["corporate_entitlements"][SID].values())
    assert [e["basis"] for e in entries] == [1000, 1000]
    assert [Decimal(e["cash"]) for e in entries] == [Decimal(260), Decimal(100)]
    assert len(result["trades"]) == 1  # No artificial trade for either allocation.
    seventh = day(result, "2020-02-07")
    position = seventh["positions"][SID]
    assert position["quantity"] == credited and position["sellable"] == 1000
    assert position["pending_shares"] == final_quantity - credited
    assert Decimal(seventh["cash"]) == 90095
    assert Decimal(seventh["equity"]) == Decimal(90095 + 260 + final_quantity * 11)
    # Independent gross book-cost invariant: the original 10000 cost less both
    # cash entitlements, split between credited inventory and pending lots.
    pending_cost = sum(
        Decimal(e["pending_cost"])
        for e in entries
        if e["event"]["share_credit_date"] and e["event"]["share_credit_date"] > "2020-02-07"
    )
    assert abs(Decimal(position["average_price"]) * credited + pending_cost - 9640) < Decimal(
        "0.00000001"
    )
    final = result["final_account"]
    assert Decimal(final["cash"]) == 90355
    assert Decimal(final["equity"]) == 90355 + final_quantity * 13
    assert (
        final["positions"][SID]["quantity"] == final["positions"][SID]["sellable"] == final_quantity
    )
    assert abs(Decimal(final["positions"][SID]["average_price"]) * final_quantity - 9640) < Decimal(
        "0.00000001"
    )
    pnl = {r["date"]: Decimal(r["pnl"]) for r in result["corporate_daily_pnl"]}
    assert abs(pnl["2020-02-07"] - (final_quantity * 11 - 1400 * 9 + 100)) < Decimal("0.000001")
    assert "nonintegral share entitlements rejected" in result["corporate_action_policy"]
    assert result["corporate_action_policy"] != manifest["execution"]["corporate_action_policy"]


@pytest.mark.parametrize(
    "fault",
    ["corporate_ex:13", "corporate_cash:13", "corporate_stock_credit:13", "after_replace:13"],
)
def test_overlapping_phase_recovery_and_completion_are_exact(tmp_path, fault):
    _, doc = overlap_fixture("0.2")
    options = dict(doc=doc, targets={"10": 1000, "12": 1000, "8": 1000, "9": 1400, "11": 1600})
    baseline = native(tmp_path, root=tmp_path / "baseline", invocation="baseline", **options)
    crash = tmp_path / "crash"
    native(tmp_path, root=crash, invocation="kill", fault=fault, expect="killed", **options)
    assert native(tmp_path, root=crash, invocation="resume", **options) == baseline
    before = (crash / "native.chk").read_bytes()
    assert native(tmp_path, root=crash, invocation="repeat", **options) == baseline
    assert (crash / "native.chk").read_bytes() == before


def test_same_record_two_plans_refused_before_native_account(tmp_path):
    _, doc = overlap_fixture()
    doc["corporate_schedule"]["rows"][1].update(record_date="2020-02-04", ex_date="2020-02-05")
    error = native(tmp_path, doc=doc, expect="refused")
    assert f"CORPORATE_SAME_RECORD_DISTRIBUTIONS_UNSUPPORTED:{SID}:2020-02-04" in error
    assert not list((tmp_path / "result").iterdir())


def test_native_duplicate_sources_do_not_duplicate_credit(tmp_path):
    _, doc = fixture()
    doc["corporate_schedule"]["rows"].append(copy.deepcopy(doc["corporate_schedule"]["rows"][0]))
    result = native(tmp_path, doc=doc)
    assert len(result["corporate_entitlements"][SID]) == 1
    assert result["final_account"]["positions"][SID]["quantity"] == 1400
    assert Decimal(result["final_account"]["cash"]) == 90255


def test_native_completed_checkpoint_rejects_source_revision(tmp_path):
    native(tmp_path)
    _, doc = fixture()
    doc["corporate_schedule"]["rows"][0]["cash_per_share"] = "0.27"
    error = native(tmp_path, doc=doc, invocation="changed", expect="refused")
    assert "identity" in error.lower()


def test_independent_audit_detects_restricted_share_and_record_basis_tampering(tmp_path):
    from ashare_quant.portfolio.audit import audit

    result = native(tmp_path)
    manifest, doc = fixture()
    data = load(tmp_path, doc, manifest)
    day(result, "2020-02-05")["positions"][SID]["sellable"] = 1400
    result["corporate_entitlements"][SID][KEY]["basis"] = 1400
    report = audit(result, data, manifest)
    assert not report["accounting_passed"]
    assert {e["code"] for e in report["errors"]} >= {
        "CORPORATE_SELLABLE",
        "CORPORATE_REGISTERED_BASIS",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "after_replace:12",
        "corporate_cash:13",
        "corporate_stock_credit:13",
        "corporate_stock_tradable:16",
    ],
)
def test_delayed_entitlements_recover_after_old_position_sold(tmp_path, fault):
    _, doc = fixture()
    doc["corporate_schedule"]["rows"][0].update(
        payment_date="2020-02-07",
        share_credit_date="2020-02-07",
        new_shares_tradable_date="2020-02-10",
    )
    options = dict(doc=doc, targets={"10": 1000, "12": 0, "8": 0})
    baseline = native(tmp_path, root=tmp_path / "baseline", invocation="baseline", **options)
    native(
        tmp_path,
        root=tmp_path / "crash",
        invocation="fault",
        fault=fault,
        expect="killed",
        **options,
    )
    assert native(tmp_path, root=tmp_path / "crash", invocation="recovery", **options) == baseline


@pytest.mark.parametrize(
    "fault",
    [
        "corporate_record:6",
        "corporate_ex:7",
        "corporate_cash:7",
        "corporate_stock_credit:7",
        "corporate_stock_tradable:10",
        "after_event:6",
        "before_replace:7",
        "after_replace:7",
    ],
)
def test_native_phase_recovery_is_exact_and_completed_repeat_is_readonly(tmp_path, fault):
    baseline_root, crash_root = tmp_path / "baseline", tmp_path / "crash"
    baseline = native(tmp_path, root=baseline_root, invocation="baseline")
    native(tmp_path, root=crash_root, invocation="fault", fault=fault, expect="killed")
    assert native(tmp_path, root=crash_root, invocation="recover") == baseline
    before = (crash_root / "native.chk").read_bytes()
    assert native(tmp_path, root=crash_root, invocation="repeat") == baseline
    assert (crash_root / "native.chk").read_bytes() == before
