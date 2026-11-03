"""Published CURRENT changes advance live operations without rebinding an in-flight reader."""
import json

from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
from ashare_quant.product.service import Product
from tests.mcp.fixture import make_deployment


def test_current_advances_live_api_and_keeps_raw_deployment_identity(tmp_path):
    from ashare_data.product.daily import LocalDailyProduct
    from ashare_data.storage.market_release import MarketRelease, atomic_write, canonical, sha_file

    config = make_deployment(tmp_path)
    document = json.loads(config.deployment.read_bytes())
    spec = document["profiles"]["product"]["market_release"]
    document["profiles"]["product"]["market_release"] = {"asset": spec["asset"], "mode": "current"}
    config.deployment.write_text(json.dumps(document))
    config = resolve(config.deployment)
    old = LocalDailyProduct.from_deployment(config.deployment)
    with TestClient(create_app(product_configuration=config)) as client:
        before = client.get("/api/v1/product/catalogue?limit=1")
        assert before.status_code == 200, before.text
        assert before.json()["receipt"]["release_id"] == 1
        release = MarketRelease(old.release.root)
        # Advance the synthetic fixture's committed pointer without changing facts.
        value = {**release.manifest, "release_id": 2, "parent_release_id": 1,
                 "parent_manifest_sha256": release.sha256}
        target = release.root / "releases/00000002.json"
        atomic_write(target, canonical(value))
        atomic_write(release.root / "CURRENT.json", canonical({
            "schema": value["schema"], "release_id": 2,
            "manifest": "releases/00000002.json", "sha256": sha_file(target),
        }))
        after = client.get("/api/v1/product/catalogue?limit=1")
        assert after.status_code == 200, after.text
        assert after.json()["receipt"]["release_id"] == 2
        assert client.get("/api/v1/product/config").json()["configured"]
        minute = client.get("/api/v1/product/minute/sources")
        assert minute.status_code == 200, minute.text
        assert minute.json()["market_release"]["release_id"] == 2
    newer = LocalDailyProduct.from_deployment(config.deployment)
    assert Product.snapshot_references(newer, config.references())["release"]["release_id"] == 2
    assert old.release.release_id == 1
    assert config.document == document
    assert config.references()["release"]["release_id"] == 1


def test_unknown_listing_intervals_do_not_become_successful_empty_universe():
    import pytest

    from ashare_quant.contracts import DomainError
    from ashare_quant.tasks.minute.demand import DemandInputs

    class Owner:
        def catalogue(self, **kwargs):
            return {"securities": [], "total": 0, "as_of": kwargs["as_of"],
                    "limitations": [], "unknown_listing_securities": ["000001.XSHE"],
                    "receipt": {"release_id": 1}}
    data = object.__new__(DemandInputs)
    data.owner, data.queries = Owner(), []
    with pytest.raises(DomainError) as error:
        data.catalogue("2024-03-04")
    assert error.value.code == "AQUANT_DIRECTORY_UNAVAILABLE"
    assert not data.queries
