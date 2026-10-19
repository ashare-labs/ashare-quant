"""Public AD Store/export -> native account regression for denied quotes."""

from copy import deepcopy

import pytest
from test_research_contract import ACCEPT, make_document, save

from ashare_quant.backtests.minute import MinuteInputs, run_backtest
from ashare_quant.backtests.minute.research import digest


def scoped_rows(bundle, metadata, *, gaps=True):
    bundle["source"]["source_sha256"] = "a" * 64
    bundle["source"]["audit_manifest_sha256"] = "c" * 64
    for i, row in enumerate(bundle["bars"]):
        conflict = row["timestamp"] == "2025-07-02T09:33:00+08:00"
        if conflict:
            row.update({k: "10.3" for k in ("open", "high", "low", "close")})
        row["native_observation"] = {
            "security": row["symbol"],
            "source_label": row["timestamp"],
            "source_file_sha256": "a" * 64,
            "source_path": "synthetic/fixture.parquet",
            "source_row": i,
            "price_status": "conflict" if conflict else "unverified",
            "diagnostics": ["test_injected_conflict"]
            if conflict
            else ["vwap_beyond_storage_envelope"],
            "execution_admitted": False,
            "new_valuation_admitted": False,
            **{k: row[k] for k in ("open", "high", "low", "close", "volume", "amount")},
        }
    if gaps:
        bundle["bars"] = [
            r for r in bundle["bars"] if r["timestamp"] != "2025-07-02T09:34:00+08:00"
        ]
        metadata["gap_policy"] = "explicit_unavailable"


def test_conflict_and_gap_keep_holdings_stale_then_recover(tmp_path):
    doc = make_document(tmp_path, ids=("601234.XSHG",), edit=scoped_rows)
    path = save(doc, tmp_path / "input.json")
    data = MinuteInputs(path, **ACCEPT)
    assert len(data.bars) == 7 and len(data.gaps) == 1
    assert data.preflight()["quote_use_policy"]["execution_denied_rows"] == 1
    assert all(
        r["use_policy"]["execution"]
        == ("deny" if r["source_label"].endswith("02T09:33:00+08:00") else "not_granted")
        for r in doc["minute"]
    )
    strategy = tmp_path / "strategy.py"
    strategy.write_text("""from jqdata import order, order_target, run_daily, get_current_data, record
def initialize(context):
    run_daily(trade, "09:31")
    run_daily(pending, "09:32")
    run_daily(check, "09:33")
    run_daily(check, "09:34")
def trade(context):
    if str(context.current_dt.date()) == "2025-07-02": order("601234.XSHG", 1000)
    else: order_target("601234.XSHG", 0)
def pending(context):
    if str(context.current_dt.date()) == "2025-07-02": order("601234.XSHG", 100)
def check(context):
    if str(context.current_dt.date()) != "2025-07-02": return
    p = context.portfolio.positions["601234.XSHG"]
    assert p.total_amount == 1000 and p.price == 10.0
    assert p.valuation["status"] == "stale" and not p.valuation["fresh_quote"]
    assert get_current_data()["601234.XSHG"].valuation["status"] == "stale"
    record(held=1000, stale=1)
""")
    result = run_backtest(strategy, path, tmp_path / "native", **ACCEPT)["result"]
    assert [(t["amount"], t["at"]) for t in result["trades"]] == [
        (1000, "2025-07-02T09:32:00+08:00"),
        (1000, "2025-07-03T09:32:00+08:00"),
    ]
    held = result["accounts"][0]["positions"]["601234.XSHG"]
    assert held["total_amount"] == 1000 and held["price"] == 10.0
    assert held["valuation"]["status"] == "stale"
    assert not result["final_account"]["positions"]


@pytest.mark.parametrize(
    "tamper", ["top_level_allow", "wrong_row", "drop_native", "drop_gap", "bad_gap_proof"]
)
def test_use_and_gap_bindings_cannot_be_overridden_by_research(tmp_path, tamper):
    doc = make_document(tmp_path, ids=("601234.XSHG",), edit=scoped_rows)
    row = next(r for r in doc["minute"] if r["use_policy"]["execution"] == "deny")
    if tamper == "top_level_allow":
        row["use_policy"]["execution"] = "not_granted"
    elif tamper == "wrong_row":
        row["evidence"]["ad_row"]["source_fields"]["native_observation"]["source_label"] = (
            "2025-07-02T09:31:00+08:00"
        )
        row["evidence"]["row_sha256"] = digest(row["evidence"]["ad_row"])
    elif tamper == "drop_native":
        row["evidence"]["ad_row"]["source_fields"].pop("native_observation")
        row["evidence"]["row_sha256"] = digest(row["evidence"]["ad_row"])
    elif tamper == "drop_gap":
        doc["unavailable_minutes"] = []
    else:
        doc["unavailable_minutes"][0]["evidence"]["coverage_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="BINDING|COVERAGE_GAP"):
        MinuteInputs(save(doc, tmp_path / "bad.json"), **ACCEPT)


def test_legacy_nested_conflict_is_still_denied(tmp_path):
    doc = make_document(
        tmp_path, ids=("601234.XSHG",), edit=lambda b, m: scoped_rows(b, m, gaps=False)
    )
    legacy = deepcopy(doc)
    for row in legacy["minute"]:
        row.pop("use_policy")
    data = MinuteInputs(save(legacy, tmp_path / "legacy.json"), **ACCEPT)
    assert data.preflight()["quote_use_policy"]["execution_denied_rows"] == 1


def test_public_export_rejects_stripping_audited_native_observation(tmp_path):
    from ashare_data import DataError

    def edit(bundle, metadata):
        scoped_rows(bundle, metadata, gaps=False)
        bundle["bars"][0].pop("native_observation")

    with pytest.raises(DataError, match="retain each native observation"):
        make_document(tmp_path, ids=("601234.XSHG",), edit=edit)
