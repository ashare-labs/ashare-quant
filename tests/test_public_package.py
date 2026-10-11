"""Public package works without private receipts or market data."""
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.m2.owner_wire import source_pin


def test_owner_identity_is_public_source_not_private_receipt():
    pin = source_pin()
    assert pin["commit"] == "0c984cdeca6c63c684e91343955701c9b4e28977"
    assert "wheel" not in pin and "review" not in pin
    assert pin["contract_review"]["scope"] == "data_production_contract_only"
    assert pin["contract_review"]["execution_permission"] is False
    assert pin["contract_review"]["receipt_sha256"] == (
        "8972635aa72609c4cf6cb2b74698d41774f0be563cdb2da9335ecb61989b4c26"
    )


def test_unconfigured_catalogue_is_explicit_and_creates_no_task(store, monkeypatch):
    monkeypatch.delenv("ASHARE_QUANT_M2_STORE", raising=False)
    with TestClient(create_app(str(store.path))) as client:
        response = client.get("/api/v1/m2/jobs/catalogue")
        assert response.status_code == 200
        value = response.json()
        assert value["contract_version"] == "m2-jobs.v6"
        assert value["configuration_error"] == "M2_JOB_NOT_CONFIGURED"
        assert not value["configured"] and not value["windows"] and value["data_binding_sha256"] is None
        assert client.get("/api/v1/m2/jobs").json() == {"items": []}
        assert client.get("/api/v1/runs").json() == []
