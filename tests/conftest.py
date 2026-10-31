import pytest

from ashare_quant.storage.store import Store


@pytest.fixture(autouse=True)
def isolated_deployment_environment(monkeypatch, tmp_path):
    """A developer's selected product must never redirect a test's temporary state."""
    for key in ("ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT", "ASHARE_QUANT_DB"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "control.sqlite3")
