"""Reference closures and explicit required inputs survive relocation unchanged."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.backup import (
    create_backup,
    reference_asset_checks,
    restore_backup,
    verify_backup,
)
from ashare_quant.deployment.runtime import doctor
from tests.deployment.test_runtime_recovery import deployment  # noqa: F401


def update(config, mutate):
    document = json.loads(config.deployment.read_text())
    mutate(document)
    config.deployment.write_text(json.dumps(document))
    return resolve(config.deployment)


@pytest.mark.parametrize("role", ["financial", "factor", "input"])
def test_explicit_required_source_role_survives_hidden_original(deployment, tmp_path, role):  # noqa: F811
    config = update(deployment, lambda d: d["assets"]["inputs"].update(role=role))
    expected = (config.asset("inputs").path / "owner-input.json").read_bytes()
    result = create_backup(config, tmp_path / "backup")
    assert not any(a["name"] == "inputs" for a in result["omitted_assets"])
    receipt = restore_backup(tmp_path / "backup", tmp_path / "restored")
    config.asset("inputs").path.rename(tmp_path / "original-input-hidden")
    restored = resolve(receipt["deployment"])
    assert (restored.asset("inputs").path / "owner-input.json").read_bytes() == expected
    report = doctor(restored)
    assert "inputs" not in report["missing"]
    assert not report["invalid_databases"] and not report["invalid_reference_assets"]
    # Runtime pins are independently reported; source edits in parallel work do
    # not turn this input-portability assertion into a runtime certification.
    assert report["ready"] == all(v["ready"] for v in report["runtime"].values())


def test_explicit_required_whole_library_needs_source_opt_in(deployment, tmp_path):  # noqa: F811
    config = update(deployment, lambda d: d["profiles"]["product"]["required_assets"].append("main"))
    with pytest.raises(DomainError, match="include-source-assets") as error:
        create_backup(config, tmp_path / "refused")
    assert error.value.code == "BACKUP_REQUIRED_SOURCE_REQUIRES_OPT_IN"
    assert not (tmp_path / "refused").exists()


def test_old_bundle_omitting_required_asset_cannot_claim_complete(deployment, tmp_path):  # noqa: F811
    bundle = tmp_path / "backup"
    create_backup(deployment, bundle)
    path = bundle / "manifest.json"
    document = json.loads(path.read_text())
    del document["paths"]["asset:inputs"]
    path.write_text(json.dumps(document))
    (bundle / "manifest.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(DomainError) as error:
        restore_backup(bundle, tmp_path / "not-created")
    assert error.value.code == "BACKUP_REQUIRED_ASSET_MISSING"
    assert not (tmp_path / "not-created").exists()


def test_missing_pinned_reference_is_rejected_before_backup(deployment, tmp_path):  # noqa: F811
    def add(document):
        for name, kind in (("market", "historical_market"), ("actions", "corporate_history")):
            document["assets"][name] = {"kind": kind, "path": name,
                "role": "corporate_source" if name == "actions" else "market_reference",
                "binding_sha256": "0" * 64}
        document["profiles"]["product"].update(default_reference_set="user-choice", reference_sets={
            "user-choice": {"kind": "historical_market", "market_asset": "market",
                            "corporate_history_asset": "actions"}})
    config = update(deployment, add)
    # An empty folder must not count as a readable reference source.
    for name in ("market", "actions"):
        config.asset(name).path.mkdir()
    report = doctor(config)
    assert set(report["invalid_reference_assets"]) == {"market", "actions"}
    assert all(x["error"] == "REFERENCE_BINDING_MISSING" for x in report["reference_assets"].values())
    with pytest.raises(DomainError) as error:
        create_backup(config, tmp_path / "refused")
    assert error.value.code == "BACKUP_REFERENCE_INVALID"
    assert not (tmp_path / "refused").exists()


def _real_references(config):
    value = os.environ.get("ASHARE_REFERENCE_PACKAGE_INVENTORY")
    if not value:
        pytest.skip("set ASHARE_REFERENCE_PACKAGE_INVENTORY to independently pinned local evidence")
    inventory = json.loads(Path(value).read_text())
    pins = {a["kind"]: a for a in inventory["assets"]}
    def add(document):
        for kind in ("daily_research", "daily_execution", "historical_market", "corporate_history"):
            record = pins[kind]
            binding = Path(record["binding_path"])
            document["assets"][kind] = {"kind": kind, "path": str(binding.parent),
                "binding": binding.name, "binding_sha256": record["binding_sha256"],
                "role": "corporate_source" if kind == "corporate_history" else "market_reference"}
        document["profiles"]["product"].update(default_reference_set="chosen-daily", reference_sets={
            "chosen-daily": {"kind": "daily_execution", "research_asset": "daily_research",
                "execution_asset": "daily_execution", "corporate_history_asset": "corporate_history"},
            "chosen-history": {"kind": "historical_market", "market_asset": "historical_market",
                "corporate_history_asset": "corporate_history"}})
    return update(config, add)


def test_real_pinned_packages_reopen_and_export_only_from_restored_roots(deployment, tmp_path):  # noqa: F811
    from ashare_data import (
        CorporateHistoryView,
        DailyExecutionView,
        DailyResearchView,
        HistoricalMarketView,
    )

    from ashare_quant.deployment.backup import files

    config = _real_references(deployment)
    original = reference_asset_checks(config)
    assert len(original) == 4 and all(v["ready"] for v in original.values())
    bundle = tmp_path / "backup"
    result = create_backup(config, bundle)
    manifest = verify_backup(bundle, expected_sha256=result["manifest_sha256"])
    assert len(manifest["reference_assets"]) == 4
    assert "asset:main" not in manifest["paths"]
    receipt = restore_backup(bundle, tmp_path / "restored")
    restored = resolve(receipt["deployment"])
    config.data_root.rename(tmp_path / "original-state-hidden")
    # Deny reopening any original source-package file. The raw source directories
    # remain untouched; this read guard proves APIs operate on relocated bytes.
    original_roots = [a.path for a in config.assets if a.kind in {
        "daily_research", "daily_execution", "historical_market", "corporate_history"}]
    original_read = Path.open
    def guarded(path, *args, **kwargs):
        assert not any(path.is_relative_to(root) for root in original_roots), str(path)
        return original_read(path, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", guarded)
        checks = reference_asset_checks(restored)
        assert all(v["ready"] for v in checks.values())
        refs = restored.references()
        r = DailyResearchView(**{"binding_path": refs["research"]["binding_path"],
                                "expected_binding_sha256": refs["research"]["binding_sha256"]})
        execution = DailyExecutionView(r, refs["execution"]["binding_path"],
                            expected_binding_sha256=refs["execution"]["binding_sha256"])
        assumptions = list(execution.descriptor()["assumptions"])
        row = execution.order_state("w2022", "000651.XSHE", as_of="2022-04-08T09:30:00+08:00",
                    side="BUY", accepted_assumptions=assumptions, cash_credit_policy="pay_date_after_close")
        assert row["admission"]["ready"] and row["row"]["upper_limit"]["value"] == "35.35"
        actions = CorporateHistoryView(refs["corporate"]["binding_path"],
                            expected_binding_sha256=refs["corporate"]["binding_sha256"])
        schedule = actions.export_aq("000651.XSHE", start="2022-04-06", end="2022-04-11",
                            tax_model="gross_cash_no_tax", accept_source_scoped_coverage=True)
        assert schedule["ready_for_declared_date_model"]
        historical = restored.references("chosen-history")["market"]
        market = HistoricalMarketView(historical["binding_path"],
                            expected_binding_sha256=historical["binding_sha256"])
        assert market.order_state("000651.XSHE", as_of="2022-04-08T09:30:00+08:00", side="SELL",
                            accept_daily_replay=True, accept_limit_model=True)["ready"]
        assert not doctor(restored)["invalid_reference_assets"]
    # Relative binding and external pins remain identical; only asset roots move.
    for name in checks:
        old, new = config.asset(name), restored.asset(name)
        assert new.binding == old.binding and new.binding_sha256 == old.binding_sha256
        assert new.path.is_relative_to(tmp_path / "restored")
        assert files(new.path)
    # Package corruption is detected even after the ordinary bundle hashes pass:
    # a mis-pinned deployment cannot certify a readable reference collection.
    pinned = restored.asset("historical_market")
    (pinned.path / pinned.binding).write_bytes(b"{}")
    assert not doctor(restored)["reference_assets"][pinned.name]["ready"]
    with pytest.raises(DomainError) as error:
        create_backup(restored, tmp_path / "refused-corrupt")
    assert error.value.code == "BACKUP_REFERENCE_INVALID"


def test_restore_can_publish_new_covered_windows_with_real_external_prices(deployment, tmp_path):  # noqa: F811
    from ashare_data.product import LocalDailyProduct

    main = os.environ.get("ASHARE_REAL_DAILY_ROOT")
    if not main:
        pytest.skip("set ASHARE_REAL_DAILY_ROOT for bounded read-only main-library verification")
    main = Path(main).resolve()
    config = _real_references(deployment)
    config = update(config, lambda d: d["assets"]["main"].update(path=str(main)))
    stat_before = (main / "daily.sqlite3").stat()
    original_roots = [a.path for a in config.assets if a.kind in {
        "daily_research", "daily_execution", "historical_market", "corporate_history"}]
    bundle = tmp_path / "backup"
    create_backup(config, bundle)
    receipt = restore_backup(bundle, tmp_path / "restored")
    restored = resolve(receipt["deployment"])
    config.data_root.rename(tmp_path / "original-state-hidden")
    original_read = Path.open
    def guarded(path, *args, **kwargs):
        assert not any(path.is_relative_to(root) for root in original_roots), str(path)
        return original_read(path, *args, **kwargs)
    results = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", guarded)
        product = LocalDailyProduct(restored.daily_root)
        for start, end in (("2020-02-03", "2020-02-14"), ("2022-04-06", "2022-04-11")):
            selection = ["000651.XSHE", "600036.XSHG"]
            coverage = product.coverage(selection, start, end, source="baostock",
                                        references=restored.references())
            assert coverage["ready"], coverage["gaps"]
            release = product.publish(restored.release_store_root, selection, start, end,
                        source="baostock", mode="covered_research", references=restored.references())
            assert release["mode"] == "covered_research"
            assert Path(release["portfolio_input_path"]).is_relative_to(tmp_path / "restored")
            assert Path(release["corporate_schedule_path"]).is_relative_to(tmp_path / "restored")
            assert release["manifest"]["receipt"]["asset"]["root"] == str(main)
            assert release["source_bindings"] == restored.reference_sets[0].source_bindings()
            results.append(release)
    (tmp_path / "restored-new-publications.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    stat_after = (main / "daily.sqlite3").stat()
    assert (stat_before.st_size, stat_before.st_mtime_ns, stat_before.st_ino) == (
        stat_after.st_size, stat_after.st_mtime_ns, stat_after.st_ino)
