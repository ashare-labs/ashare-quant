"""Synthetic protocol doubles test refusal/binding only, never real-data acceptance."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.portfolio.inputs import digest as input_digest
from ashare_quant.portfolio.product_reference import owner_digest, source_bindings, validate
from ashare_quant.product.contracts import ProductReleaseRequest
from ashare_quant.product.publication import check_coverage, consume, scope
from ashare_quant.storage.artifacts import canonical, sha
from tests.portfolio.test_corporate_actions import fixture


@pytest.fixture
def protocol(tmp_path, monkeypatch):
    _, document = fixture()
    schedule = document.pop("corporate_schedule")
    document.pop("corporate_account_model")
    document.pop("corporate_source_replacement")
    refs = {"name": "explicit-test-double", "kind": "daily_execution", **{
        role: {"binding_path": str(tmp_path / role / "binding.json"), "binding_sha256": pin * 64}
        for role, pin in (("research", "b"), ("execution", "c"), ("corporate", "a"))}}
    bindings = source_bindings(refs)
    query = ProductReleaseRequest(securities=document["securities"], start="2020-02-03",
                                  end="2020-02-10", reference_set=refs["name"])
    selected = scope(query)
    identity = {k: refs[k] for k in ("name", "kind")}
    days = [d for d in document["calendar"] if selected["start"] <= d <= selected["end"]]
    document.update(producer={"name": "ashare-data", "owner_api": "ashare_data.product.LocalDailyProduct"},
                    real_market_data=True, state_mode="retrospective_model")
    document["limitations"].append("SYNTHETIC PROTOCOL DOUBLE; not real market evidence")
    document["owner_api_reads"] = [{"operation": "synthetic-test-only"}]
    index = {(r["security"], r["date"]): r for r in document["daily"]}
    document["owner_history_sha256"] = {
        d: input_digest({s: [index[s, t]["close"] for t in document["calendar"][
            document["calendar"].index(d)-120:document["calendar"].index(d)]] for s in query.securities})
        for d in days}
    rows = []
    for row in document["daily"]:
        if row["date"] not in days:
            continue
        state = {"security": row["security"], "date": row["date"]}
        for field in ("suspended", "is_st", "reference_price", "upper_limit", "lower_limit"):
            value = False if field == "is_st" else row["close"] if field == "reference_price" else row[field]
            state[field] = {"value": value, "status": "source_observed",
                            "source_refs": ["SYNTHETIC TEST ONLY"], "assumption_ids": []}
        rows.append(state)
        row["state_evidence"] = {"reference_sha256": owner_digest(state)}
    document["reference_evidence"] = evidence = {
        "schema": "ad-product-reference-evidence.v1",
        "scope": {k: selected[k] for k in ("securities", "start", "end")},
        "reference_set": identity, "source_bindings": bindings,
        "market": {"kind": "daily_execution", "rows": rows, "accepted_assumptions": [],
            "cash_credit_policy": "pay_date_before_open",
            "report": {"binding_sha256": "c" * 64, "research_binding_sha256": "b" * 64,
                       "historical_pit": False, "execution_permission": False},
            "admission": {"ready": True, "purpose": "retrospective_simulation", "issues": [],
                "required_assumptions": [], "accepted_assumptions": [],
                "cash_credit_policy": "pay_date_before_open", "requests": [
                    {"security": r["security"], "date": r["date"], "side": side}
                    for r in rows for side in ("BUY", "SELL")]}},
        "corporate": {"binding_sha256": "a" * 64, "response_sha256": owner_digest(schedule)},
    }
    coverage = {"schema": "ad-local-daily-coverage.v1", "scope": selected, "ready": True,
                "gaps": [], "assumptions": [], "limitations": document["limitations"],
                "reference_set": identity, "source_bindings": bindings,
                "owner_api_reads": document["owner_api_reads"],
                "reference_evidence_sha256": owner_digest(evidence)}
    coverage["coverage_sha256"] = owner_digest(coverage)
    files = {"input.json": canonical(document), "corporate-schedule.json": canonical(schedule),
             "coverage.json": canonical(coverage)}
    manifest = {"schema": "ad-local-daily-product.v1", "securities": selected["securities"],
                "start": selected["start"], "end": selected["end"], "source_selection": "auto",
                "mode": "covered_research", "reference_set": identity, "source_bindings": bindings,
                "coverage_sha256": coverage["coverage_sha256"]}
    for prefix, name in (("portfolio_input", "input.json"), ("corporate_schedule", "corporate-schedule.json"),
                         ("coverage", "coverage.json")):
        manifest[prefix + "_path"] = name
        manifest[prefix + "_bytes"] = len(files[name])
        manifest["coverage_file_sha256" if prefix == "coverage" else prefix + "_sha256"] = sha(files[name])
    dataset = owner_digest(manifest)
    config = SimpleNamespace(release_store_root=tmp_path / "releases", control_db=tmp_path / "state/control.sqlite3")
    root = config.release_store_root / "local_daily_products" / dataset
    root.mkdir(parents=True)
    files["manifest.json"] = canonical(manifest)
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    release = {"dataset_id": dataset, "manifest": manifest, "mode": "covered_research",
               "reference_set": identity, "source_bindings": bindings,
               "portfolio_input_path": str(root / "input.json"),
               "portfolio_input_sha256": sha(files["input.json"]),
               "corporate_schedule_path": str(root / "corporate-schedule.json"),
               "corporate_schedule_sha256": sha(files["corporate-schedule.json"]),
               "coverage_sha256": coverage["coverage_sha256"]}

    class View:
        portfolio_input_path = root / "input.json"
        def describe(self):
            return json.loads((root / "manifest.json").read_bytes())
        def read(self):
            return json.loads((root / "input.json").read_bytes())
        def read_corporate_schedule(self):
            return json.loads((root / "corporate-schedule.json").read_bytes())
        def read_coverage(self):
            return json.loads((root / "coverage.json").read_bytes())

    # Deliberately no owner validation: AQ must independently reject mismatches.
    monkeypatch.setattr("ashare_data.Store", lambda path: SimpleNamespace(local_daily_product=lambda _: View()))
    return release, query, config, refs, root, document, coverage


def test_projection_preserves_owner_bytes_and_is_concurrent_idempotent(protocol):
    release, query, config, refs, root, _, _ = protocol
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda _: consume(release, query, config, refs), range(3)))
    assert len(set(paths)) == 1
    doc = json.loads(paths[0].read_bytes())
    assert doc["product_release"]["portfolio_input_sha256"] == sha(before["input.json"])
    assert doc["product_release"]["corporate_schedule_sha256"] == sha(before["corporate-schedule.json"])
    assert doc["corporate_schedule"] == json.loads(before["corporate-schedule.json"])
    assert paths[0].parent.name == sha(paths[0].read_bytes())
    assert (paths[0].parent / "base-input.json").read_bytes() == before["input.json"]
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert not config.control_db.exists()  # No task, account, or native execution.


@pytest.mark.parametrize("change", ["input", "schedule", "coverage", "manifest", "path", "mode", "bindings"])
def test_release_mismatch_never_creates_consumer_state(protocol, change):
    release, query, config, refs, root, _, _ = protocol
    if change in {"input", "schedule", "coverage", "manifest"}:
        name = {"input": "input.json", "schedule": "corporate-schedule.json",
                "coverage": "coverage.json", "manifest": "manifest.json"}[change]
        value = json.loads((root / name).read_bytes())
        value["tampered"] = True
        (root / name).write_bytes(canonical(value))
    elif change == "path":
        release["portfolio_input_path"] = str(config.control_db.parent / "outside.json")
    elif change == "mode":
        release["mode"] = "price_only_no_limits"
    else:
        refs["execution"]["binding_sha256"] = "f" * 64
    with pytest.raises(DomainError) as error:
        consume(release, query, config, refs)
    assert error.value.code == "PRODUCT_OWNER_CONTRACT_MISMATCH"
    assert not config.control_db.parent.exists()


@pytest.mark.parametrize("change", ["scope", "bindings", "digest", "reference", "missing_reads"])
def test_coverage_identity_cannot_be_rebound(protocol, change):
    _, query, _, refs, _, _, original = protocol
    value = copy.deepcopy(original)
    if change == "scope":
        value["scope"]["securities"] = ["600036.XSHG"]
    elif change == "bindings":
        value["source_bindings"]["execution"]["binding_sha256"] = "e" * 64
    elif change == "reference":
        value["reference_set"]["name"] = "unselected"
    elif change == "missing_reads":
        value["owner_api_reads"] = []
    else:
        value["coverage_sha256"] = "0" * 64
    if change != "digest":
        value["coverage_sha256"] = owner_digest({k: v for k, v in value.items() if k != "coverage_sha256"})
    with pytest.raises(DomainError, match="Published source identity differs"):
        check_coverage(value, query, refs)


@pytest.mark.parametrize("change", ["missing", "value", "hash", "source", "unknown", "admission",
                                   "side", "corporate", "scope", "manifest_scope", "semantic",
                                   "coverage_evidence"])
def test_structured_reference_cannot_be_replaced_by_a_limit_origin_label(protocol, change):
    release, query, config, refs, _, _, _ = protocol
    path = consume(release, query, config, refs)
    doc = json.loads(path.read_bytes())
    evidence = doc["reference_evidence"]
    state = evidence["market"]["rows"][0]
    row = next(r for r in doc["daily"] if (r["security"], r["date"]) == (state["security"], state["date"]))
    row["limit_origin"] = "renamed to look authoritative"
    if change == "missing":
        del doc["reference_evidence"]
    elif change == "value":
        row["upper_limit"] = "999"
    elif change == "hash":
        row["state_evidence"]["reference_sha256"] = "0" * 64
    elif change in {"source", "unknown"}:
        if change == "source":
            state["upper_limit"]["source_refs"] = []
        else:
            state["upper_limit"]["status"] = "unavailable"
        row["state_evidence"]["reference_sha256"] = owner_digest(state)
    elif change == "admission":
        evidence["market"]["admission"]["ready"] = False
    elif change == "side":
        evidence["market"]["admission"]["requests"].pop()
    elif change == "corporate":
        evidence["corporate"]["binding_sha256"] = "e" * 64
    elif change == "scope":
        evidence["scope"]["start"] = "2025-01-01"
    elif change == "manifest_scope":
        doc["product_release"]["manifest"]["start"] = "1900-01-01"
        doc["product_release"]["dataset_id"] = owner_digest(doc["product_release"]["manifest"])
    elif change == "semantic":
        doc["product_release"]["portfolio_input_semantic_sha256"] = "0" * 64
    else:
        evidence["market"]["extra_source_claim"] = "Changed after release"
    with pytest.raises(ValueError, match="PRODUCT_REFERENCE_CONFLICT"):
        validate(doc)


def test_dataset_modes_come_from_the_input_and_old_descriptors_remain_readable(protocol):
    from ashare_quant.tasks.portfolio.data import Datasets

    release, query, config, refs, _, _, _ = protocol
    path = consume(release, query, config, refs)
    datasets = Datasets(config.control_db.parent / "test-only-tasks")
    item = datasets.import_file(path, "Synthetic protocol double")
    assert item.mode == "covered_research" and not item.diagnostic_allowed
    stored = datasets.root / item.dataset_id / "descriptor.json"
    old = json.loads(stored.read_bytes())
    old.pop("mode")
    old.pop("diagnostic_allowed")
    stored.write_bytes(canonical(old))
    before = stored.read_bytes()
    restored, _ = datasets.read(item.dataset_id)
    assert restored == item and stored.read_bytes() == before
