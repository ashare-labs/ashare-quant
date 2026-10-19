"""Protocol tests, separate from installed-wheel, real-source acceptance."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize
from ashare_quant.product import service
from ashare_quant.product.contracts import ProductMinuteSelection

P = "/api/v1/product/minute"
QUERY = {"security": "600000.XSHG", "start": "2026-06-02T09:30:00+08:00",
         "end": "2026-06-02T09:31:00+08:00", "source": "neigezhu"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from ashare_data.product import LocalMinuteProduct
    from ashare_data.storage.local_daily import LocalDailyStore

    entry = tmp_path / "deployment.json"
    entry.write_text(json.dumps({"schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {"main": {"kind": "local_daily", "path": "library", "role": "market"},
                   "releases": {"kind": "store", "path": "releases", "role": "publication"}},
        "profiles": {"product": {"kind": "product", "daily_asset": "main",
            "release_store_asset": "releases", "control_db": "state/control.sqlite3",
            "paper_root": "state/paper"}},
    }))
    config = resolve(entry)
    LocalDailyStore(config.daily_root).close()
    initialize(config)
    # Only the runtime/reader responses are protocol doubles here.
    monkeypatch.setattr(service, "verify_data_runtime", lambda: None)
    calls = []

    def prices(self, security, start, end, *, source="tdx"):
        calls.append((self.entry, security, start.isoformat(), end.isoformat(), source))
        return {"source": source, "rows": [{"high": "NaN", "price_status": "invalid",
                "source_row": 7, "use_decision": {"execution": "deny"}}],
                "report": {"execution_admitted": False}}

    monkeypatch.setattr(LocalMinuteProduct, "prices", prices)
    with TestClient(create_app(product_configuration=config)) as session:
        yield session, calls, config


def test_explicit_routing_preserves_quality_and_default_tdx(client):
    session, calls, config = client
    response = session.post(P + "/prices", json=QUERY)
    assert response.status_code == 200, response.text
    assert calls[-1] == (config.deployment, QUERY["security"], QUERY["start"], QUERY["end"], "neigezhu")
    assert response.json()["rows"][0] == {"high": "NaN", "price_status": "invalid",
                                         "source_row": 7, "use_decision": {"execution": "deny"}}
    default = session.post(P + "/prices", json={k: v for k, v in QUERY.items() if k != "source"})
    assert default.json()["source"] == "tdx"
    listing = session.get(P + "/sources")
    assert listing.status_code == 200
    assert listing.json()["default_source"] == "tdx"
    assert listing.headers["cache-control"] == "no-store"


def test_closed_request_rejects_override_paths_and_cross_origin(client):
    session, calls, _ = client
    for extra in ({"entry": "/tmp/other.json"}, {"accept_research": True}, {"repair": True}):
        assert session.post(P + "/prices", json={**QUERY, **extra}).status_code == 422
    assert session.post(P + "/prices?source=tdx", json=QUERY).status_code == 422
    assert session.get(P + "/sources?root=/tmp").status_code == 422
    assert session.post(P + "/prices", json=QUERY,
                        headers={"origin": "https://untrusted.invalid"}).status_code == 403
    assert calls == []


@pytest.mark.parametrize("override", [
    {"start": "2026-06-02T09:30"}, {"start": "2026-06-02T09:30:01+08:00"},
    {"end": "2026-09-02T09:31:00+08:00"}, {"security": "920000.XBSE"},
])
def test_invalid_window_or_rewritten_native_identity_is_rejected(override):
    with pytest.raises(ValidationError):
        ProductMinuteSelection(**{**QUERY, **override})


def test_exact_native_bj_identity_is_supported():
    assert ProductMinuteSelection(**{**QUERY, "security": "BJ.920000"}).security == "BJ.920000"
