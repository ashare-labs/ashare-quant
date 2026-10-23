"""User-supplied real data only; never substitute synthetic rows for native acceptance."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support.app import create_app
from tests.support.daily.executor import Configuration


@pytest.fixture(scope="session")
def owner_store(tmp_path_factory):
    components = os.getenv("ASHARE_QUANT_TEST_DAILY_COMPONENTS")
    if not components:
        pytest.skip("Composed real M2 checks require explicit owner components; not executed")
    from ashare_data import Store

    from ashare_quant.runtime import runtime_fingerprint
    from tests.support.legacy_provider.owner_wire import verify_runtime

    runtime_fingerprint(native=True)
    verify_runtime()
    root = tmp_path_factory.mktemp("composed-owner")
    owner = Store.init(root / "producer")
    market = owner.import_portable_component(Path(components) / "market")
    facts = owner.import_portable_component(Path(components) / "facts")
    request = dict(
        price_dataset_id=market,
        facts_component_id=facts,
        window_ids=["two_day", "w1", "w2"],
        mode="conditional_research",
    )
    report = owner.validate_portable(**request).to_dict()
    assert report["status"] != "BLOCKED", report
    product = owner.compose_portable(**request).to_dict()
    owner.export_portable(product["dataset_id"], root / "export")
    fresh = Store.init(root / "consumer")
    assert fresh.import_portable(root / "export") == product["dataset_id"]
    return root / "consumer", product["dataset_id"]


@pytest.fixture
def host(owner_store, tmp_path):
    path, dataset = owner_store
    app = create_app(str(tmp_path / "control.sqlite3"), daily_tasks_configuration=Configuration(path))
    with TestClient(app) as client:
        response = client.post("/api/v1/tasks/daily/data/bind", json={"dataset_id": dataset})
        assert response.status_code == 200, response.text
        yield app.state.daily_tasks, client


def request(client, window="w1", key="test", parameters=None):
    c = client.get("/api/v1/tasks/daily/jobs/catalogue").json()
    w = next(w for w in c["windows"] if w["scope"]["window_id"] == window)
    return dict(
        window_id=window,
        name="Portable acceptance",
        idempotency_key=key,
        assumption_ack_sha256=w["assumption_ack_sha256"],
        data_binding_sha256=c["data_binding_sha256"],
        parameters=parameters or c["parameters"],
    )


def create(client, window="w1", key="test", parameters=None):
    response = client.post("/api/v1/tasks/daily/jobs", json=request(client, window, key, parameters))
    assert response.status_code == 201, response.text
    return response.json()
