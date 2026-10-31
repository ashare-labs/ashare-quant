"""Public package works without private receipts or market data."""

from importlib.metadata import version

from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.providers.runtime import pin as source_pin


def test_owner_identity_is_public_source_not_private_receipt():
    pin = source_pin()
    assert len(pin["commit"]) == 40
    assert pin["source_verification"] == "committed_candidate_content"
    assert pin["version"] == version("ashare-data")
    assert pin["portable_owner_version"] == "0.10.0.dev1"
    assert "wheel" not in pin and "review" not in pin
    assert pin["contract_review"]["scope"] == "data_production_contract_only"
    assert pin["contract_review"]["execution_permission"] is False
    assert pin["contract_review"]["status"] == "not_reviewed"
    assert pin["contract_review"]["receipt_sha256"] is None


def test_unconfigured_workbench_never_selects_sample_data(store, monkeypatch):
    monkeypatch.delenv("ASHARE_QUANT_DAILY_STORE", raising=False)
    with TestClient(create_app(str(store.path))) as client:
        assert client.get("/api/v1/tasks/daily/jobs/catalogue").status_code == 404
        assert client.get("/api/v1/portfolio/catalogue").status_code == 404
        value = client.get("/api/v1/python/catalogue")
        assert value.status_code == 503
        assert client.get("/api/v1/python/jobs").json() == {"items": []}
        assert client.get("/api/v1/history/jobs").json() == {"items": []}
        assert client.get("/api/v1/runs").json() == []
