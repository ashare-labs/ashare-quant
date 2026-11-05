"""A stalled market read must not disable local strategy storage or fan out I/O."""

import json
from datetime import datetime
from threading import Event
from time import monotonic

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.product.discovery import DiscoveryRead
from tests.support.strategy_files import PREFIX as PREFIX
from tests.support.strategy_files import client as client
from tests.support.strategy_files import configuration as configuration


def test_one_inflight_read_and_no_completed_environment_cache():
    read = DiscoveryRead(timeout=0.01)
    entered, release = Event(), Event()
    calls = []

    def stalled():
        calls.append("old")
        entered.set()
        assert release.wait(3)
        return "old-release"

    try:
        for _ in range(3):
            with pytest.raises(DomainError) as error:
                read.run(stalled)
            assert error.value.code == "CATALOGUE_DATA_TIMEOUT"
        assert entered.is_set() and calls == ["old"]
    finally:
        release.set()
    assert read._pending.result(timeout=3) == "old-release"
    assert read.run(lambda: "new-release") == "new-release"


def test_python_metadata_timeout_preserves_files_and_reports_unknown_data(client, monkeypatch):
    app, entered, release = client.app, Event(), Event()
    app.state.python_catalogue_read.timeout = 0.02
    product = app.state.product
    full_configured, calls = product.configured, []

    def unavailable_market():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return full_configured()

    monkeypatch.setattr(product, "configured", unavailable_market)
    try:
        for _ in range(2):
            start = monotonic()
            response = client.get(PREFIX + "/catalogue")
            assert monotonic() - start < 2
            assert response.status_code == 200, response.text
            value = response.json()
            assert len(value["environment_id"]) == 64
            assert value["data_availability"] is None
            assert value["data_status"]["status"] == "unavailable"
            assert value["data_status"]["error"]["code"] == "CATALOGUE_DATA_TIMEOUT"
            assert value["data_status"]["execution_verified"] is False
            assert value["frequencies"] == ["1m", "1d"]
        assert entered.is_set() and calls == [1]
        source = "raise RuntimeError('saved only; never run')\n"
        assert client.put(PREFIX + "/files", json={"path": "local.py", "source": source}).status_code == 200
        listing = client.get(PREFIX + "/directories").json()["entries"]
        assert listing[0]["path"] == "local.py"
        assert datetime.fromisoformat(listing[0]["modified_at"]).utcoffset().total_seconds() == 0
        assert client.get(PREFIX + "/files?path=local.py").json()["source"] == source
        assert app.state.python_jobs.repo.rows() == []
    finally:
        release.set()
        app.state.python_catalogue_read._pending.exception(timeout=5)


def test_factor_definition_discovery_survives_unavailable_environment(client, monkeypatch):
    jobs, entered, release = client.app.state.factor_jobs, Event(), Event()
    jobs.catalogue_read.timeout = 0.01
    calls = []

    def stalled():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return "old-environment"

    monkeypatch.setattr(jobs, "environment", stalled)
    try:
        for _ in range(2):
            response = client.get("/api/v1/factors/catalogue")
            assert response.status_code == 200
            value = response.json()
            assert len(value["items"]) == 368 and value["environment_id"] is None
            assert value["data_status"]["error"]["code"] == "CATALOGUE_DATA_TIMEOUT"
        assert calls == [1] and entered.is_set()
        assert client.get("/api/v1/factors/catalogue?version=unknown").status_code == 422
    finally:
        release.set()
        jobs.catalogue_read._pending.result(timeout=3)
    monkeypatch.setattr(jobs, "environment", lambda: "b" * 64)
    recovered = client.get("/api/v1/factors/catalogue").json()
    assert recovered["environment_id"] == "b" * 64
    assert recovered["data_status"]["status"] == "available"


def test_local_storage_reloads_on_deployment_change(client, configuration):
    original = configuration.deployment.read_bytes()
    changed = json.loads(original)
    changed["profiles"]["product"]["control_db"] = "other/control.sqlite3"
    configuration.deployment.write_text(json.dumps(changed))
    response = client.get(PREFIX + "/directories")
    assert response.status_code != 409
    configuration.deployment.write_bytes(original)
    assert client.get(PREFIX + "/directories").status_code == 200


def test_factor_environment_selection_never_opens_market_metadata(client, monkeypatch):
    product = client.app.state.product

    def forbidden(*args, **kwargs):
        raise AssertionError("Source identity must not instantiate a market reader")

    monkeypatch.setattr(product, "owner", forbidden)
    assert len(client.app.state.factor_jobs.environment()) == 64


def test_pointer_only_factor_discovery_still_checks_pinned_owner_runtime(client, monkeypatch):
    def changed_runtime():
        raise DomainError("DATA_RUNTIME_MISMATCH", "Paired owner runtime changed", 409)

    monkeypatch.setattr("ashare_quant.product.service.verify_data_runtime", changed_runtime)
    response = client.get("/api/v1/factors/catalogue")
    assert response.status_code == 200
    value = response.json()
    assert value["environment_id"] is None
    assert value["data_status"]["error"]["code"] == "DATA_RUNTIME_MISMATCH"
    with pytest.raises(DomainError) as refused:
        client.app.state.factor_jobs.environment()
    assert refused.value.code == "DATA_RUNTIME_MISMATCH"
