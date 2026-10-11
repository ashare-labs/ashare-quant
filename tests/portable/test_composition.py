"""Owner production failures stay owner failures; validation-only anchors never grant reads."""

import copy
import json
import os
from pathlib import Path

import pytest
from ashare_data import DataError, Store

from ashare_quant.contracts import DomainError
from ashare_quant.m2.portable import bind_dataset
from ashare_quant.m2.portable_policy import validate_plan


def recipe(kind):
    directory = Path(os.environ["ASHARE_QUANT_TEST_M2_COMPONENTS"]) / kind
    value = json.loads((directory / "component.json").read_text())
    for doc in value["documents"]:
        doc["artifacts"] = {
            key: {"path": str(directory / ref["path"])} for key, ref in doc["artifacts"].items()
        }
    return {key: value[key] for key in ("kind", "classification", "documents", "claims")}


@pytest.mark.parametrize("defect", ["synthetic", "calendar_missing", "fact_unknown"])
def test_public_validate_and_compose_do_not_fill_gaps(owner_store, tmp_path, defect):
    owner = Store.init(tmp_path / "refusal-owner")
    market, facts = recipe("market"), recipe("facts")
    if defect == "synthetic":
        market["classification"] = "synthetic"
    elif defect == "calendar_missing":
        market["documents"] = [d for d in market["documents"] if d["role"] != "calendar"]
    else:
        facts["claims"]["facts"][0]["status"] = "unknown"
    request = dict(
        price_dataset_id=owner.import_m2_sources(**market),
        facts_component_id=owner.import_m2_sources(**facts),
        window_ids=["m2a", "w1", "w2"],
        mode="conditional_research",
    )
    report = owner.validate_m2(**request).to_dict()
    assert report["status"] == "BLOCKED" and report["gaps"]
    assert owner.m2_report(report["report_id"]).to_dict() == report
    with pytest.raises(DataError) as exc:
        owner.compose_m2(**request)
    assert exc.value.code == "M2_COMPOSITION_BLOCKED"
    assert exc.value.details["report_id"] == report["report_id"]
    assert owner.m2_snapshots() == []


def test_dependency_table_is_preserved_without_expanding_read_roles(owner_store):
    path, dataset = owner_store
    binding = bind_dataset(Store(path), dataset)
    for entry in binding.entries:
        plan = json.loads(entry.owner_binding.window_plan)
        dependencies = plan["owner_validation_dependencies"]
        assert dependencies["consumer_read_permission"] is False
        assert not any(r["target_date"] == "2019-12-31" for r in plan["read_roles"])
        assert all(d.trading_date.isoformat() >= "2020-01-03" for d in entry.windows[0].days)
        changed = copy.deepcopy(plan)
        changed["owner_validation_dependencies"]["consumer_read_permission"] = True
        with pytest.raises(DomainError) as exc:
            validate_plan(changed)
        assert exc.value.code == "M2_VALIDATION_DOMAIN_NOT_READABLE"
        changed = copy.deepcopy(plan)
        changed["read_roles"].append(
            dict(
                current_date="2020-01-03",
                target_date="2019-12-31",
                phase="DECIDE",
                role="previous",
                operations=["decision_prev_close"],
            )
        )
        with pytest.raises(DomainError):
            validate_plan(changed)


def test_component_original_missing_is_owner_refusal(owner_store, tmp_path):
    owner = Store.init(tmp_path / "missing-owner")
    missing = recipe("market")
    missing["documents"][0]["artifacts"]["raw"]["path"] = str(tmp_path / "absent-original")
    with pytest.raises(DataError):
        owner.import_m2_sources(**missing)
    assert owner.m2_snapshots() == []


@pytest.mark.parametrize(
    "defect",
    [
        "preclose_exponent",
        "preclose_whitespace",
        "preclose_nonnumeric",
        "volume_overlong",
        "event_unknown_security",
        "event_missing_fields",
    ],
)
def test_dev2_malformed_inputs_never_publish(owner_store, tmp_path, defect):
    """Replay PM2-R1/R2 through public APIs; mutate only private temporary copies."""
    owner = Store.init(tmp_path / "invalid-owner")
    market, facts = recipe("market"), recipe("facts")
    if defect.startswith("preclose") or defect == "volume_overlong":
        doc = next(d for d in market["documents"] if d["name"] == "legacy-daily")
        raw = json.loads(Path(doc["artifacts"]["raw"]["path"]).read_text())
        row = next(row for row in raw["rows"] if row["date"] == "2020-01-03")
        if defect == "volume_overlong":
            row["volume"] = "0" * 5000
        else:
            row["preclose"] = {
                "preclose_exponent": "12.47e0",
                "preclose_whitespace": " 12.4700 ",
                "preclose_nonnumeric": "oops",
            }[defect]
        mutated = tmp_path / "mutated-source.json"
        mutated.write_text(json.dumps(raw))
        doc["artifacts"]["raw"] = {"path": str(mutated)}
    elif defect == "event_unknown_security":
        facts["claims"]["events"][0].update(entitled_security=None, record_date="2020-01-06")
    else:
        facts["claims"]["events"] = [
            {
                "entitled_security": None,
                "evidence": copy.deepcopy(facts["claims"]["events"][0]["evidence"]),
            }
        ]
    try:
        request = dict(
            price_dataset_id=owner.import_m2_sources(**market),
            facts_component_id=owner.import_m2_sources(**facts),
            window_ids=["m2a"],
            mode="conditional_research",
        )
        report = owner.validate_m2(**request).to_dict()
    except DataError as exc:
        assert exc.code in {
            "M2_COMPONENT_SCHEMA",
            "M2_FACT_MISSING",
            "M2_RELATED_EVENT",
            "M2_SOURCE_CONFLICT",
            "M2_DECIMAL",
            "M2_UNIT_UNKNOWN",
            "M2_INTEGER",
        }
    else:
        assert report["status"] == "BLOCKED" and report["gaps"]
        assert owner.m2_report(report["report_id"]).to_dict() == report
        with pytest.raises(DataError) as exc:
            owner.compose_m2(**request)
        assert exc.value.code == "M2_COMPOSITION_BLOCKED"
    assert owner.m2_snapshots() == []
