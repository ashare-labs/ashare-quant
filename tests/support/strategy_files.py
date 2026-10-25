"""Shared strategy files fixtures; synthetic evidence only."""

import json

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
from ashare_quant.paper.service import PaperService
from ashare_quant.storage.store import Store

PREFIX = "/api/v1/python"

@pytest.fixture
def configuration(tmp_path):
    document = {
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "daily": {"kind": "local_daily", "path": "daily", "role": "market"},
            "release": {"kind": "store", "path": "release", "role": "publication"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "daily", "release_store_asset": "release",
            "control_db": "state/control.sqlite3", "paper_root": "state/paper",
        }},
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = resolve(path)
    Store(config.control_db)
    PaperService(config.paper_root)
    return config

@pytest.fixture
def client(configuration):
    with TestClient(create_app(product_configuration=configuration)) as value:
        yield value

def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
