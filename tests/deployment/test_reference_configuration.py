"""Reference selection is explicit and read-only; no market observations are invented."""

import json

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve


def document():
    return {
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "library", "role": "market"},
            "release": {"kind": "store", "path": "releases", "role": "publication"},
            **{name: {"kind": kind, "path": name, "role": role, "binding_sha256": pin * 64}
               for name, kind, role, pin in (
                   ("research", "daily_research", "market_reference", "a"),
                   ("execution", "daily_execution", "market_reference", "b"),
                   ("market", "historical_market", "market_reference", "c"),
                   ("corporate", "corporate_history", "corporate_source", "d"),
               )},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "main", "release_store_asset": "release",
            "control_db": "state/control.sqlite3", "paper_root": "state/paper",
            "reference_sets": {
                "daily": {"kind": "daily_execution", "research_asset": "research",
                          "execution_asset": "execution", "corporate_history_asset": "corporate"},
                "historical": {"kind": "historical_market", "market_asset": "market",
                               "corporate_history_asset": "corporate"},
            },
            "default_reference_set": "daily",
        }},
    }


def test_reference_selection_does_not_create_missing_assets(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document()))
    config = resolve(path)
    assert {p.name for p in tmp_path.iterdir()} == {"deployment.json"}
    assert config.default_reference_set == "daily"
    assert config.references() == {
        "name": "daily", "kind": "daily_execution",
        **{role: {"binding_path": str(tmp_path / role / "binding.json"), "binding_sha256": pin * 64}
           for role, pin in (("research", "a"), ("execution", "b"), ("corporate", "d"))},
    }
    assert config.references("historical")["market"]["binding_sha256"] == "c" * 64
    assert set(config.selected_assets) == {"main", "release", "research", "execution", "market", "corporate"}
    assert config.reference_sets[0].source_bindings()["execution"] == {
        "kind": "daily_execution", "binding_sha256": "b" * 64,
    }
    with pytest.raises(DomainError) as refused:
        config.references("absent")
    assert refused.value.code == "PRODUCT_REFERENCE_SET_UNKNOWN"


@pytest.mark.parametrize("change", [
    "default", "no_default", "binding_absolute", "binding_escape", "binding_invalid",
    "hash", "missing_hash", "role", "wrong_kind", "unknown_key", "writer_overlap",
])
def test_invalid_reference_configuration_is_closed_without_files(tmp_path, change):
    value = document()
    profile = value["profiles"]["product"]
    asset = value["assets"]["research"]
    if change == "default":
        profile["default_reference_set"] = "absent"
    elif change == "no_default":
        del profile["default_reference_set"]
    elif change.startswith("binding_"):
        asset["binding"] = {"binding_absolute": "/tmp/binding.json",
                            "binding_escape": "../binding.json", "binding_invalid": ""}[change]
    elif change == "hash":
        asset["binding_sha256"] = "bad"
    elif change == "missing_hash":
        del asset["binding_sha256"]
    elif change == "role":
        asset["role"] = "publication"
    elif change == "wrong_kind":
        profile["reference_sets"]["daily"]["research_asset"] = "execution"
    elif change == "unknown_key":
        profile["reference_sets"]["daily"]["fallback"] = "historical"
    elif change == "writer_overlap":
        profile["control_db"] = "research/control.sqlite3"
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    with pytest.raises(DomainError) as refused:
        resolve(path)
    assert refused.value.code == "DEPLOYMENT_INVALID"
    assert {p.name for p in tmp_path.iterdir()} == {"deployment.json"}


def test_released_references_follow_price_pin_and_keep_legacy_selection(tmp_path):
    from ashare_data.product.configuration import resolve as owner_resolve

    from ashare_quant.portfolio.product_reference import source_bindings

    value = document()
    value["assets"]["released"] = {"kind": "market_release", "path": "formal", "role": "market"}
    profile = value["profiles"]["product"]
    profile["market_release"] = {"asset": "released", "release_id": 10, "manifest_sha256": "f" * 64}
    profile["reference_sets"]["formal"] = {"kind": "market_release"}
    profile["default_reference_set"] = "formal"
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    config = resolve(path)
    owner = owner_resolve(path)
    assert config.references() == owner["references"]
    assert config.references()["release"] == owner["market_release"]
    assert source_bindings(config.references()) == config.reference_sets[-1].source_bindings()
    assert config.references("daily")["kind"] == "daily_execution"
    assert {p.name for p in tmp_path.iterdir()} == {"deployment.json"}
    # Separate reference roots/releases are not accepted beneath a price release.
    profile["reference_sets"]["formal"]["release_id"] = 9
    path.write_text(json.dumps(value))
    with pytest.raises(DomainError, match="profile price release"):
        resolve(path)
