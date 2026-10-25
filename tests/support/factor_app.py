"""Shared factor app fixtures; synthetic evidence only."""

import pandas as pd
import pytest

from ashare_quant.api import create_app
from ashare_quant.storage.artifacts import atomic, sha


@pytest.fixture
def source(tmp_path):
    days = pd.bdate_range("2024-01-02", periods=12).strftime("%Y-%m-%d").tolist()
    close = [[10 + i, 30 - i] for i in range(12)]
    payload = dict(
        schema="alpha191.panel.v1",
        sessions=days,
        securities=["000001.XSHE", "600000.XSHG"],
        universe=[[True, True] for _ in days],
        fields={"close": close},
        adjustment="raw",
        units={"price": "CNY/share", "volume": "share", "amount": "CNY"},
    )
    path = tmp_path / "prices.json"
    atomic(path, payload)
    return {
        "fixture": dict(
            kind="alpha191", label="人工线性价格夹具", path=str(path), sha256=sha(path.read_bytes())
        )
    }, payload

@pytest.fixture
def app(tmp_path, source):
    return create_app(str(tmp_path / "control.sqlite3"), factor_sources=source[0])

def request(app, **changes):
    return dict(
        environment_id=app.state.factor_jobs.environment(),
        idempotency_key="factor-request",
        name="Five-day difference",
        factor_id="alpha191:alpha014",
        definition="gtja191.explicit.v1",
        source_id="fixture",
        securities=["000001.XSHE", "600000.XSHG"],
        history_start="2024-01-02",
        start_date="2024-01-02",
        end_date="2024-01-17",
        **changes,
    )
