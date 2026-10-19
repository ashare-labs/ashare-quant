"""Real installed owner/runtime, tiny synthetic files, no market library or account."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize

P = "/api/v1/product"
QUERY = {
    "security": "600000.XSHG",
    "start": "2026-06-03T09:30:00+08:00",
    "end": "2026-06-03T09:30:00+08:00",
}


def write(path, value):
    path.write_text(json.dumps(value))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path):
    from ashare_data.storage.local_daily import LocalDailyStore

    for name, value in (("a", "10"), ("b", "20"), ("same", "10")):
        write(
            tmp_path / f"{name}.json",
            {
                "schema": "ad-tdx-minute-session.v1",
                "minute": [
                    {
                        "security": QUERY["security"],
                        "source_label": QUERY["start"],
                        "open": value,
                        "evidence": "synthetic configuration snapshot fixture",
                    }
                ],
            },
        )
    (tmp_path / "selected.json").symlink_to(tmp_path / "a.json")
    doc = {
        "schema": "ashare-deployment.v1",
        "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "daily", "role": "market"},
            "releases": {"kind": "store", "path": "releases", "role": "publication"},
            "minute-tdx": {
                "kind": "reference",
                "path": "selected.json",
                "role": "bounded_research",
            },
            "minute-hf-download": {
                "kind": "reference",
                "path": "unused-hf",
                "role": "bounded_research",
            },
        },
        "profiles": {
            "product": {
                "kind": "product",
                "daily_asset": "main",
                "release_store_asset": "releases",
                "control_db": "state/control.sqlite3",
                "paper_root": "state/paper",
                "minute_sources": {
                    "tdx": {
                        "reader": "tdx-session",
                        "asset": "minute-tdx",
                        "content": {"sha256": sha(tmp_path / "a.json")},
                        "coverage": "synthetic",
                        "limits": [],
                    },
                    "neigezhu": {
                        "reader": "neigezhu-native",
                        "asset": "minute-hf-download",
                        "manifest": {"path": "unused.csv", "sha256": "a" * 64},
                        "review": {"path": "unused.json", "sha256": "b" * 64},
                        "coverage": "synthetic unused registration",
                        "limits": [],
                    },
                },
            }
        },
    }
    entry = tmp_path / "deployment.json"
    write(entry, doc)
    config = resolve(entry)
    LocalDailyStore(config.daily_root).close()
    initialize(config)
    other = copy.deepcopy(doc)
    other["assets"]["minute-tdx"]["path"] = "b.json"
    other["profiles"]["product"]["minute_sources"]["tdx"]["content"]["sha256"] = sha(
        tmp_path / "b.json"
    )
    return config, doc, other


def ready(client):
    response = client.get(P + "/config")
    assert response.status_code == 200 and response.json()["configured"], response.text
    prices = client.post(P + "/minute/prices", json=QUERY)
    assert prices.status_code == 200, prices.text
    return response.json(), prices.json()


def refused(client):
    for response in (
        client.get(P + "/minute/sources"),
        client.post(P + "/minute/prices", json=QUERY),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PRODUCT_CONFIGURATION_CHANGED"
        assert response.json()["error"]["details"]["restart_required"] is True
        assert "rows" not in response.json()
    config = client.get(P + "/config").json()
    assert config["configured"] is False
    assert config["error"]["code"] == "PRODUCT_CONFIGURATION_CHANGED"
    return config


@pytest.mark.parametrize(
    "change",
    [
        "path_and_pin",
        "path_only",
        "content_pin",
        "manifest_pin",
        "review_pin",
        "symlink",
        "missing",
        "malformed",
    ],
)
def test_live_changes_require_restart_and_cannot_mix_old_identity_with_new_data(case, change):
    config, original, other = case
    updated = copy.deepcopy(original)
    source = updated["profiles"]["product"]["minute_sources"]
    if change == "path_and_pin":
        updated = other
    elif change == "path_only":
        updated["assets"]["minute-tdx"]["path"] = "same.json"
    elif change == "content_pin":
        source["tdx"]["content"]["sha256"] = "c" * 64
    elif change in {"manifest_pin", "review_pin"}:
        source["neigezhu"][change.removesuffix("_pin")]["sha256"] = "c" * 64
    with TestClient(create_app(product_configuration=config)) as client:
        before, prices = ready(client)
        assert prices["rows"][0]["open"] == "10"
        if change == "missing":
            config.deployment.unlink()
        elif change == "malformed":
            config.deployment.write_text("{")
        elif change == "symlink":
            link = config.data_root / "selected.json"
            link.unlink()
            link.symlink_to(config.data_root / "same.json")
        else:
            write(config.deployment, updated)
        after = refused(client)
        assert after["configuration_id"] == before["configuration_id"]
        assert after["assets"] == before["assets"]
        # Restoring bytes alone does not silently unlatch a running service.
        write(config.deployment, original)
        refused(client)
    if change not in {"missing", "malformed"}:
        write(config.deployment, updated)
        with TestClient(create_app(product_configuration=resolve(config.deployment))) as restarted:
            description = restarted.get(P + "/config").json()
            assert description["configured"]
            assert description["configuration_id"] != before["configuration_id"]
            response = restarted.post(P + "/minute/prices", json=QUERY)
            if change == "content_pin":
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "PRODUCT_MINUTE_INTEGRITY"
            else:
                assert response.status_code == 200, response.text
                assert response.json()["rows"][0]["open"] == (
                    "20" if change == "path_and_pin" else "10"
                )
                path = next(
                    a["path"] for a in description["assets"] if a.get("name") == "minute-tdx"
                )
                assert response.json()["binding"]["path"] == path


def test_change_during_minute_read_never_returns_an_observation(case, monkeypatch):
    from ashare_data.product import LocalMinuteProduct

    config, _, other = case
    original_read = LocalMinuteProduct.prices

    def change_while_reading(self, *args, **kwargs):
        result = original_read(self, *args, **kwargs)
        write(config.deployment, other)
        return result

    monkeypatch.setattr(LocalMinuteProduct, "prices", change_while_reading)
    with TestClient(create_app(product_configuration=config)) as client:
        response = client.post(P + "/minute/prices", json=QUERY)
        assert response.status_code == 409, response.text
        refused(client)


def test_changed_owner_document_is_rejected_even_if_file_is_restored(case, monkeypatch):
    from ashare_data.product import LocalMinuteProduct

    config, original, other = case
    original_init = LocalMinuteProduct.__init__

    def change_while_opening(self, *args, **kwargs):
        write(config.deployment, other)
        original_init(self, *args, **kwargs)
        write(config.deployment, original)

    monkeypatch.setattr(LocalMinuteProduct, "__init__", change_while_opening)
    with TestClient(create_app(product_configuration=config)) as client:
        refused(client)


def test_fixed_config_formatting_and_source_failure_do_not_require_restart(case):
    config, original, _ = case
    with TestClient(create_app(product_configuration=config)) as client:
        before, _ = ready(client)
        config.deployment.write_text(json.dumps(original, indent=4, sort_keys=True))
        after, _ = ready(client)
        assert after["configuration_id"] == before["configuration_id"]
        assert (
            client.post(P + "/minute/prices", json={**QUERY, "source": "auto"}).status_code == 200
        )
        for source in ("missing", "neigezhu"):
            response = client.post(P + "/minute/prices", json={**QUERY, "source": source})
            assert response.status_code == 422
            assert response.json()["error"]["code"] != "PRODUCT_CONFIGURATION_CHANGED"
        assert ready(client)[1]["source"] == "tdx"


def test_concurrent_old_snapshot_cannot_clear_latched_change(case, monkeypatch):
    import ashare_quant.deployment as deployment

    config, original, other = case
    product = create_app(product_configuration=config).state.product
    captured, release = Event(), Event()

    def delayed_resolve(*args, **kwargs):
        value = resolve(*args, **kwargs)
        if current_thread().name.startswith("old-snapshot"):
            captured.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(deployment, "resolve", delayed_resolve)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-snapshot") as pool:
        future = pool.submit(product.require_configuration_snapshot, product.configuration)
        try:
            assert captured.wait(5)
            write(config.deployment, other)
            with pytest.raises(DomainError, match="restart the API"):
                product.require_configuration_snapshot(product.configuration)
            write(config.deployment, original)
        finally:
            release.set()
        with pytest.raises(DomainError, match="restart the API"):
            future.result(timeout=5)
