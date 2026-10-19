import pytest

from ashare_quant.contracts import RunCreate, StrategyCreate
from ashare_quant.storage.store import Store
from ashare_quant.tasks.synthetic.service import Service


@pytest.fixture(autouse=True)
def isolated_deployment_environment(monkeypatch, tmp_path):
    """A developer's selected product must never redirect a test's temporary state."""
    for key in ("ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT", "ASHARE_QUANT_DB"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "control.sqlite3")


@pytest.fixture
def queued(store):
    def create(scenario="buy_hold", key="test-1", account="paper-test"):
        service = Service(store)
        version = service.register_strategy(StrategyCreate(name="Manual spec", scenario=scenario))
        return service.submit(RunCreate(strategy_version_id=version.version_id,
                                        idempotency_key=key, account_id=account))
    return create
