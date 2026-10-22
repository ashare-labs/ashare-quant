import pytest

pytest.importorskip("mcp", reason="Install the optional [mcp] extra for adapter tests")


@pytest.fixture
def anyio_backend():
    return "asyncio"
