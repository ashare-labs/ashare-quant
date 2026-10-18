"""Public package works without private receipts or market data."""
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.providers.daily.owner_wire import source_pin


def test_owner_identity_is_public_source_not_private_receipt():
    pin = source_pin()
    assert pin["commit"] == "1591e6df219a0632dc4baa30ca32069cd97eb381"
    assert pin["version"] == "0.10.0.dev5"
    assert pin["portable_owner_version"] == "0.10.0.dev1"
    assert "wheel" not in pin and "review" not in pin
    assert pin["contract_review"]["scope"] == "data_production_contract_only"
    assert pin["contract_review"]["execution_permission"] is False
    assert pin["contract_review"]["status"] == "not_reviewed"
    assert pin["contract_review"]["receipt_sha256"] is None


def test_unconfigured_catalogue_is_explicit_and_creates_no_task(store, monkeypatch):
    monkeypatch.delenv("ASHARE_QUANT_DAILY_STORE", raising=False)
    with TestClient(create_app(str(store.path))) as client:
        response = client.get("/api/v1/tasks/daily/jobs/catalogue")
        assert response.status_code == 200
        value = response.json()
        assert value["contract_version"] == "m2-jobs.v6"
        assert value["configuration_error"] == "M2_JOB_NOT_CONFIGURED"
        assert not value["configured"] and not value["windows"] and value["data_binding_sha256"] is None
        assert client.get("/api/v1/tasks/daily/jobs").json() == {"items": []}
        assert client.get("/api/v1/runs").json() == []
