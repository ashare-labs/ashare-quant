"""Public packaging must refuse absent research materials without a fake result."""

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.m2.owner_wire import release


def test_missing_owner_receipt_is_an_explicit_refusal():
    with pytest.raises(DomainError) as caught:
        release()
    assert caught.value.code == "M2_OWNER_RELEASE_UNAVAILABLE"
    assert caught.value.status == 503


def test_missing_executor_cannot_be_advertised_as_an_available_job_catalogue(store):
    with TestClient(create_app(str(store.path))) as client:
        response = client.get("/api/v1/m2/jobs/catalogue")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "M2_JOB_ARTIFACT_UNAVAILABLE"
        assert client.get("/api/v1/runs").json() == []
