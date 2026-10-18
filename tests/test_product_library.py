"""Opt-in real-library acceptance, never synthetic price or receipt substitution.

Set ASHARE_QUANT_TEST_PRODUCT_ROOT to an existing AD local daily library and use
the paired installed AD wheel. Only bounded publications and isolated AQ state
are created below pytest's temporary directory; the source library stays read-only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize

P = "/api/v1/product"
Q = "/api/v1/portfolio"
SECURITIES = ["000651.XSHE", "600036.XSHG", "601398.XSHG"]


@pytest.fixture
def library_configuration(tmp_path):
    value = os.getenv("ASHARE_QUANT_TEST_PRODUCT_ROOT")
    if not value:
        pytest.skip("Caller-supplied real local daily library and paired AD wheel are required")
    source = Path(value).expanduser().resolve()
    assert (source / "daily.sqlite3").is_file()
    manifest = tmp_path / "deployment.json"
    manifest.write_text(json.dumps({
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": str(source), "role": "market"},
            "releases": {"kind": "store", "path": "releases", "role": "publication"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
            "control_db": "aq/control.sqlite3", "paper_root": "aq/paper",
        }},
    }))
    configuration = resolve(manifest)
    initialize(configuration)
    return configuration


@pytest.mark.parametrize("start,end", [
    ("2020-02-03", "2020-02-07"), ("2024-01-02", "2024-01-05"),
])
def test_real_non_sample_release_worker_and_restart(library_configuration, start, end):
    from ashare_data.product import LocalDailyProduct

    config = library_configuration
    query = {"securities": SECURITIES, "start": start, "end": end, "source": "auto"}
    with TestClient(create_app(product_configuration=config)) as client:
        configuration = client.get(P + "/config")
        assert configuration.status_code == 200 and configuration.json()["configured"], configuration.text
        catalogue = client.get(P + "/catalogue?limit=3")
        assert catalogue.status_code == 200 and catalogue.json()["total"] > 2, catalogue.text
        search = client.get(P + "/catalogue?query=600036").json()
        assert "600036.XSHG" in {item["security"] for item in search["securities"]}
        prices = client.post(P + "/prices", json=query)
        assert prices.status_code == 200, prices.text
        observed = prices.json()
        assert not observed["gaps"] and {row["security"] for row in observed["rows"]} == set(SECURITIES)
        assert observed["receipt"]["asset"]["root"] == str(config.daily_root)
        assert observed["receipt"]["asset"]["database"] == "daily.sqlite3"
        assert observed["receipt"]["responses"] and not observed["receipt"]["network_used"]
        public_owner = LocalDailyProduct(config.daily_root).prices(**query)
        assert observed["rows"] == public_owner["rows"]
        assert observed["receipt"]["rows_sha256"] == public_owner["receipt"]["rows_sha256"]
        published = client.post(P + "/releases", json=query)
        assert published.status_code == 201, published.text
        descriptor = published.json()["dataset"]
        assert descriptor["real_market_data"] and descriptor["source_refs"]
        assert descriptor["securities"] == SECURITIES
        input_path = Path(published.json()["release"]["portfolio_input_path"])
        assert input_path.is_relative_to(config.release_store_root)
        assert client.get(Q + "/catalogue").json()["datasets"] == [descriptor]

        selection = {
            "dataset_id": descriptor["dataset_id"], "securities": SECURITIES,
            "start_date": start, "end_date": end,
        }
        covered = client.post(Q + "/preflight", json=selection).json()
        assert not covered["ready"] and covered["gaps"]
        diagnostic = selection | {"mode": "price_only_no_limits"}
        unacknowledged = client.post(Q + "/preflight", json=diagnostic).json()
        assert not unacknowledged["ready"]
        assert any(gap["field"] == "retrospective_state_opt_in" for gap in unacknowledged["gaps"])
        diagnostic["accept_retrospective_state"] = True
        preflight = client.post(Q + "/preflight", json=diagnostic).json()
        assert preflight["ready"], preflight
        strategy = client.post(Q + "/strategies", json={"algorithm": "equal-weight"}).json()
        request = diagnostic | {
            "name": "Real source acceptance", "idempotency_key": "real-source",
            "strategy_version_id": strategy["version_id"],
            "assumption_ack_sha256": preflight["assumption_ack_sha256"],
        }
        wrong_ack = client.post(Q + "/jobs", json=request | {"assumption_ack_sha256": "0" * 64})
        assert wrong_ack.status_code == 409
        assert client.get(Q + "/jobs").json()["items"] == []
        accepted = client.post(Q + "/jobs", json=request)
        assert accepted.status_code == 201, accepted.text
        job_id = accepted.json()["job_id"]
        worker = subprocess.run([
            sys.executable, "-m", "ashare_quant.deployment", "--deployment", str(config.deployment),
            "task-worker", "--task-id", job_id,
        ], capture_output=True, text=True, timeout=180)
        assert worker.returncode == 0, worker.stdout + worker.stderr
        worker_state = json.loads(worker.stdout)
        assert worker_state["control_db"] == str(config.control_db)
        assert worker_state["job"]["status"] == "succeeded"
        result = client.get(Q + "/jobs/" + job_id + "/result")
        assert result.status_code == 200, result.text
        completed = result.json()
        assert completed["audit"]["passed"] and completed["summary"]["trade_count"] > 0
        assert {trade["security"] for trade in completed["trades"]} == set(SECURITIES)
        assert completed["economic_scope"] == "price_only_no_limits_not_total_return"

    with TestClient(create_app(product_configuration=config)) as restarted:
        assert restarted.get(P + "/config").json()["configuration_id"] == configuration.json()["configuration_id"]
        assert restarted.get(Q + "/jobs/" + job_id + "/result").json() == completed
        assert restarted.get(Q + "/catalogue").json()["datasets"] == [descriptor]
    assert not (config.data_root / "daily.sqlite3").exists()


def test_real_missing_observations_are_not_filled_or_published(library_configuration):
    config = library_configuration
    with TestClient(create_app(product_configuration=config)) as client:
        selection = {"securities": ["600036.XSHG"], "start": "1900-01-01", "end": "1900-01-05"}
        prices = client.post(P + "/prices", json=selection)
        assert prices.status_code == 200, prices.text
        assert prices.json()["gaps"] and prices.json()["rows"] == []
        refused = client.post(P + "/releases", json=selection)
        assert refused.status_code == 422 and refused.json()["error"]["origin"] == "ashare-data"
        assert client.get(Q + "/catalogue").json()["datasets"] == []
        assert client.get(Q + "/jobs").json()["items"] == []
