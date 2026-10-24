import pytest
from fastapi.testclient import TestClient

from ashare_quant.access import configured_origins
from ashare_quant.api import create_app

ORIGIN = "http://100.99.71.60:41846"


def test_default_host_and_origin_remain_local(tmp_path):
    with TestClient(create_app(str(tmp_path / "control.sqlite3"))) as client:
        assert client.get("/api/v1/health", headers={"Host": "100.99.71.60:41846"}).status_code == 400
        assert client.post("/api/v1/python/jobs", json={}, headers={"Origin": ORIGIN}).status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/python/jobs", "/api/v1/python/directories", "/api/v1/product/prices"])
def test_explicit_origin_passes_only_that_origin_and_retains_host_checks(tmp_path, path):
    app = create_app(str(tmp_path / "control.sqlite3"), trusted_browser_origins=(ORIGIN,))
    with TestClient(app) as client:
        headers = {"Host": "100.99.71.60:41846", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
        assert client.get("/api/v1/health", headers=headers).status_code == 200
        # Invalid bodies cannot mutate state; 422 proves normal validation was reached.
        assert client.post(path, json={}, headers=headers).status_code == 422
        for origin in ("http://100.99.71.61:41846", "http://100.99.71.60:41847",
                       "https://100.99.71.60:41846", "http://100.99.71.60:41846.evil.test", "null"):
            assert client.post(path, json={}, headers=headers | {"Origin": origin}).status_code == 403
        assert client.get("/api/v1/health", headers=headers | {"Host": "evil.test"}).status_code == 400
        assert client.post(path, json={}, headers={"Origin": "http://localhost:41846"}).status_code == 422
        assert client.post(path, json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


@pytest.mark.parametrize("origin", ["*", "http://*.example", "ftp://host", "http://user@host",
                                    "http://host/path", "http://host?query", "http://host#fragment",
                                    "http://host:0", "http://host:999999", "http://host\\evil", " http://host"])
def test_configuration_rejects_broad_or_non_origin_values(origin):
    with pytest.raises(ValueError):
        configured_origins((origin,))
