"""HTTP protocol doubles only; real-library acceptance lives in test_product_library."""

import json
import sys
from dataclasses import replace
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.contracts import DataQuery, DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize
from ashare_quant.market.reader import DailyResearchRow, ResearchCoverageQuery, ResearchQuery
from ashare_quant.product import service
from ashare_quant.tasks.portfolio.contracts import PortfolioDataset

P = "/api/v1/product"
SELECTION = {
    "securities": ["600036.XSHG", "000651.XSHE", "601398.XSHG"],
    "start": "2024-01-02", "end": "2024-01-05", "source": "auto",
}


@pytest.fixture
def configuration(tmp_path):
    from ashare_data.storage.local_daily import LocalDailyStore

    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "library", "role": "market"},
            "releases": {"kind": "store", "path": "releases", "role": "publication"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
            "control_db": "state/control.sqlite3", "paper_root": "state/paper",
        }},
    }))
    configuration = resolve(path)
    # Empty schema for protocol doubles; no invented observations or receipts.
    LocalDailyStore(configuration.daily_root).close()
    initialize(configuration)
    return configuration


@pytest.fixture
def owner_protocol(monkeypatch):
    """Only record delegation; these empty responses are never market acceptance."""
    module = ModuleType("ashare_data.product")
    calls = []

    class Owner:
        def __init__(self, root):
            calls.append(("open", root))

        def catalogue(self, **request):
            calls.append(("catalogue", request))
            return {"schema": "test-double", "securities": [], "receipt": {}, **request}

        def prices(self, securities, start, end, source):
            calls.append(("prices", securities, start, end, source))
            return {"schema": "test-double", "rows": [], "gaps": [], "receipt": {}}

    module.LocalDailyProduct = Owner
    monkeypatch.setitem(sys.modules, "ashare_data.product", module)
    monkeypatch.setattr(service, "verify_data_runtime", lambda: None)
    return Owner, calls


def test_missing_configuration_never_selects_sample_data(tmp_path):
    with TestClient(create_app(str(tmp_path / "control.sqlite3"))) as client:
        config = client.get(P + "/config").json()
        assert not config["configured"] and config["default_dataset_id"] is None
        assert config["error"]["code"] == "DEPLOYMENT_NOT_CONFIGURED"
        for response in (
            client.get(P + "/catalogue"),
            client.post(P + "/prices", json=SELECTION),
            client.post(P + "/releases", json=SELECTION),
        ):
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_CONFIGURED"
            assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/v1/portfolio/catalogue").json()["datasets"] == []


def test_environment_selects_same_database_and_explicit_store_for_api_worker(
    configuration, owner_protocol, monkeypatch
):
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(configuration.deployment))
    monkeypatch.setenv("ASHARE_QUANT_DB", str(configuration.data_root / "wrong.sqlite3"))
    monkeypatch.setenv("ASHARE_QUANT_DAILY_STORE", str(configuration.data_root / "wrong-owner"))
    app = create_app()
    assert app.state.store.path == configuration.control_db
    assert app.state.daily_tasks.repo.store.path == app.state.portfolio_jobs.repo.store.path
    assert app.state.daily_tasks.config.owner_store == configuration.owner_store_root
    with TestClient(app) as client:
        before = client.get(P + "/config").json()
        assert before["configured"] and before["profile"] == "product"
        assert before["capabilities"]["covered_research"] is False
        assert client.post(P + "/prices", json=SELECTION).status_code == 200
        assert owner_protocol[1][-1] == (
            "prices", SELECTION["securities"], SELECTION["start"], SELECTION["end"], "auto"
        )
    with TestClient(create_app()) as reopened:
        after = reopened.get(P + "/config").json()
        assert after["configuration_id"] == before["configuration_id"]
    assert not (configuration.data_root / "wrong.sqlite3").exists()


def test_invalid_manifest_cannot_create_default_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(tmp_path / "missing.json"))
    with TestClient(create_app()) as client:
        response = client.get(P + "/config")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPLOYMENT_INVALID"
    assert not (tmp_path / ".state").exists()


def test_explicit_database_mismatch_closes_product(configuration, owner_protocol):
    wrong = configuration.data_root / "other.sqlite3"
    with TestClient(create_app(str(wrong), product_configuration=configuration)) as client:
        response = client.get(P + "/config")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_STATE_MISMATCH"
        response = client.post(P + "/releases", json=SELECTION)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_STATE_MISMATCH"
    assert not owner_protocol[1]
    assert not wrong.exists()


def test_selected_product_requires_explicit_initialization(configuration, owner_protocol):
    absent = configuration.data_root / "uninitialized" / "control.sqlite3"
    config = replace(configuration, control_db=absent)
    with TestClient(create_app(product_configuration=config)) as client:
        response = client.get(P + "/config")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPLOYMENT_NOT_INITIALIZED"
    assert not absent.parent.exists()
    assert not owner_protocol[1]


def test_explicit_database_cannot_bypass_selected_environment(
    configuration, owner_protocol, monkeypatch
):
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(configuration.deployment))
    wrong = configuration.data_root / "other-state" / "control.sqlite3"
    with TestClient(create_app(str(wrong))) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_STATE_MISMATCH"
    assert not wrong.parent.exists()
    assert not owner_protocol[1]


@pytest.mark.parametrize("code", ["DATA_RUNTIME_MISMATCH", "RUNTIME_DEPENDENCY_MISMATCH"])
def test_runtime_identity_errors_are_explicit(configuration, owner_protocol, monkeypatch, code):
    def refused():
        raise DomainError(code, "Installed source or runtime differs", 409)

    monkeypatch.setattr(service, "verify_data_runtime", refused)
    with TestClient(create_app(product_configuration=configuration)) as client:
        config = client.get(P + "/config").json()
        assert not config["configured"] and config["error"]["code"] == code
        for response in (
            client.get(P + "/catalogue"), client.post(P + "/prices", json=SELECTION),
            client.post(P + "/releases", json=SELECTION),
        ):
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == code
    assert not owner_protocol[1]


def test_owner_missing_api_is_a_configuration_error(configuration, owner_protocol, monkeypatch):
    monkeypatch.setitem(sys.modules, "ashare_data.product", None)
    with TestClient(create_app(product_configuration=configuration)) as client:
        config = client.get(P + "/config").json()
        assert not config["configured"] and config["error"]["code"] == "PRODUCT_OWNER_API_UNAVAILABLE"
        response = client.get(P + "/catalogue")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PRODUCT_OWNER_API_UNAVAILABLE"


def test_owner_data_gaps_and_storage_failures_are_not_500(
    configuration, owner_protocol, monkeypatch
):
    from ashare_data import DataError

    def gap(*args, **kwargs):
        raise DataError("PRODUCT_MISSING_DAILY", "No observation", {"security": "600036.XSHG"})

    monkeypatch.setattr(owner_protocol[0], "prices", gap)
    with TestClient(create_app(product_configuration=configuration)) as client:
        response = client.post(P + "/prices", json=SELECTION)
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "PRODUCT_MISSING_DAILY", "message": "No observation",
            "details": {"security": "600036.XSHG"}, "origin": "ashare-data",
        }

        def unavailable(*args, **kwargs):
            raise OSError("asset unavailable")

        monkeypatch.setattr(owner_protocol[0], "prices", unavailable)
        response = client.post(P + "/prices", json=SELECTION)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PRODUCT_DATA_UNAVAILABLE"


def test_strict_requests_and_local_origin(configuration, owner_protocol):
    with TestClient(create_app(product_configuration=configuration)) as client:
        assert client.get(P + "/catalogue?query=600036&limit=20&offset=2").status_code == 200
        assert owner_protocol[1][-1] == ("catalogue", {"query": "600036", "limit": 20, "offset": 2})
        for change in (
            {"start": "2025-01-01"}, {"securities": ["600036.XSHG", "600036.XSHG"]},
            {"securities": []}, {"unknown": True},
        ):
            assert client.post(P + "/prices", json=SELECTION | change).status_code == 422
        assert client.post(P + "/prices?unknown=1", json=SELECTION).status_code == 422
        assert client.get(P + "/catalogue?limit=501").status_code == 422
        assert client.get(P + "/config?sample=true").status_code == 422
        response = client.post(P + "/releases", json=SELECTION,
                               headers={"Origin": "https://example.invalid"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PRODUCT_ORIGIN_REFUSED"


def test_publication_imports_into_the_same_portfolio_service(
    configuration, owner_protocol, monkeypatch
):
    path = configuration.release_store_root / "input.json"
    release = {"dataset_id": "b" * 64, "portfolio_input_path": str(path), "manifest": {}}
    calls = []

    def publish(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return release

    monkeypatch.setattr(owner_protocol[0], "publish", publish, raising=False)
    app = create_app(product_configuration=configuration)
    descriptor = PortfolioDataset(
        dataset_id="a" * 64, name="Protocol double", input_sha256="c" * 64,
        securities=SELECTION["securities"], trading_dates=[], real_market_data=False,
        state_mode="test_double_not_market_evidence", action_coverage={},
        source_refs=["test-double"], limitations=["Not real market evidence"],
    )

    def importing(selected, name):
        calls.append((selected, name))
        return descriptor

    monkeypatch.setattr(app.state.portfolio_jobs, "import_data", importing)
    with TestClient(app) as client:
        response = client.post(P + "/releases", json=SELECTION | {"name": "Chosen window"})
        assert response.status_code == 201, response.text
        assert response.json() == {"release": release, "dataset": descriptor.model_dump(mode="json")}
        assert calls == [
            ((configuration.release_store_root, SELECTION["securities"], "2024-01-02", "2024-01-05"),
             {"source": "auto"}),
            (path, "Chosen window"),
        ]
        release["portfolio_input_path"] = str(configuration.data_root / "outside.json")
        response = client.post(P + "/releases", json=SELECTION)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_OWNER_CONTRACT_MISMATCH"


def test_state_selection_changes_the_configuration_identity(configuration, owner_protocol):
    with TestClient(create_app(product_configuration=configuration)) as client:
        before = client.get(P + "/config").json()["configuration_id"]
    other = replace(configuration, control_db=configuration.data_root / "other.sqlite3")
    initialize(other)
    with TestClient(create_app(product_configuration=other)) as client:
        after = client.get(P + "/config").json()["configuration_id"]
    assert before != after


def test_research_read_contract_is_general_but_fixed_evaluation_is_unchanged():
    assert len(ResearchQuery(securities=SELECTION["securities"]).securities) == 3
    assert len(ResearchCoverageQuery(**{key: SELECTION[key] for key in (
        "securities", "start", "end"
    )}).securities) == 3
    row = DailyResearchRow(security="600036.XSHG", source_date="2024-01-02",
                           source_label_at="2024-01-02T15:00:00+08:00")
    assert row.security == "600036.XSHG"
    with pytest.raises(ValidationError):
        DataQuery(dataset_id="fixed-evaluation", securities=["600036.XSHG"], frequency="daily",
                  start="2024-01-02T00:00:00+08:00", end="2024-01-02T15:00:00+08:00",
                  as_of="2024-01-03T00:00:00+08:00", visibility="verified")
