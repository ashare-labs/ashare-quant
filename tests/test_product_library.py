"""Opt-in real-library acceptance, never synthetic price or receipt substitution.

Set ASHARE_QUANT_TEST_PRODUCT_ROOT to an existing AD local daily library and use
the paired installed AD wheel. Only bounded publications and isolated AQ state
are created below pytest's temporary directory; the source library stays read-only.
"""

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
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
    manifest.write_text(
        json.dumps(
            {
                "schema": "ashare-deployment.v1",
                "data_root": ".",
                "assets": {
                    "main": {"kind": "local_daily", "path": str(source), "role": "market"},
                    "releases": {"kind": "store", "path": "releases", "role": "publication"},
                },
                "profiles": {
                    "product": {
                        "kind": "product",
                        "daily_asset": "main",
                        "release_store_asset": "releases",
                        "control_db": "aq/control.sqlite3",
                        "paper_root": "aq/paper",
                    }
                },
            }
        )
    )
    market_root = os.getenv("ASHARE_QUANT_TEST_MARKET_ROOT")
    if market_root:
        from ashare_data.storage.market_release import MarketRelease

        release = MarketRelease(market_root)
        document = json.loads(manifest.read_text())
        document["assets"]["market"] = {
            "kind": "market_release",
            "path": str(release.root),
            "role": "market",
        }
        document["profiles"]["product"]["market_release"] = {
            "asset": "market",
            "release_id": release.release_id,
            "manifest_sha256": release.sha256,
        }
        document["profiles"]["product"]["default_minute_source"] = "canonical"
        manifest.write_text(json.dumps(document))
    configuration = resolve(manifest)
    initialize(configuration)
    return configuration


@pytest.mark.parametrize(
    "start,end",
    [
        ("2020-02-03", "2020-02-07"),
        ("2024-01-02", "2024-01-05"),
    ],
)
def test_real_non_sample_release_worker_and_restart(library_configuration, start, end):
    from ashare_data.product import LocalDailyProduct

    config = library_configuration
    query = {"securities": SECURITIES, "start": start, "end": end, "source": "auto"}
    with TestClient(create_app(product_configuration=config)) as client:
        configuration = client.get(P + "/config")
        assert configuration.status_code == 200 and configuration.json()["configured"], (
            configuration.text
        )
        catalogue = client.get(P + "/catalogue?limit=3")
        assert catalogue.status_code == 200 and catalogue.json()["total"] > 2, catalogue.text
        search = client.get(P + "/catalogue?query=600036").json()
        assert "600036.XSHG" in {item["security"] for item in search["securities"]}
        prices = client.post(P + "/prices", json=query)
        assert prices.status_code == 200, prices.text
        observed = prices.json()
        assert not observed["gaps"] and {row["security"] for row in observed["rows"]} == set(
            SECURITIES
        )
        released = config.document["profiles"]["product"].get("market_release")
        if released:
            assert observed["receipt"]["release"]["manifest_sha256"] == released["manifest_sha256"]
            with sqlite3.connect(
                (config.daily_root / "daily.sqlite3").as_uri() + "?mode=ro", uri=True
            ) as original:
                for row in observed["rows"]:
                    symbol = ("sh" if row["security"].endswith(".XSHG") else "sz") + row[
                        "security"
                    ][:6]
                    raw = original.execute(
                        "select open,high,low,close,volume,amount from bars where symbol=? and date=? and source=?",
                        (symbol, row["date"], row["source"]),
                    ).fetchone()
                    assert raw is not None
                    for field, value in zip(
                        ("open", "high", "low", "close", "volume", "amount"), raw
                    ):
                        assert (None if value is None else Decimal(value)) == (
                            None if row[field] is None else Decimal(row[field])
                        )
        else:
            assert observed["receipt"]["asset"]["root"] == str(config.daily_root)
            assert observed["receipt"]["asset"]["database"] == "daily.sqlite3"
        assert observed["receipt"]["responses"] and not observed["receipt"]["network_used"]
        public_owner = LocalDailyProduct.from_deployment(config.deployment).prices(**query)
        assert observed["rows"] == public_owner["rows"]
        assert observed["receipt"]["rows_sha256"] == public_owner["receipt"]["rows_sha256"]
        published = client.post(P + "/releases", json=query | {"mode": "price_only_no_limits"})
        assert published.status_code == 201, published.text
        descriptor = published.json()["dataset"]
        assert descriptor["real_market_data"] and descriptor["source_refs"]
        assert descriptor["securities"] == SECURITIES
        input_path = Path(published.json()["release"]["portfolio_input_path"])
        assert input_path.is_relative_to(config.release_store_root)
        assert client.get(Q + "/catalogue").json()["datasets"] == [descriptor]

        selection = {
            "dataset_id": descriptor["dataset_id"],
            "securities": SECURITIES,
            "start_date": start,
            "end_date": end,
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
            "name": "Real source acceptance",
            "idempotency_key": "real-source",
            "strategy_version_id": strategy["version_id"],
            "assumption_ack_sha256": preflight["assumption_ack_sha256"],
        }
        wrong_ack = client.post(Q + "/jobs", json=request | {"assumption_ack_sha256": "0" * 64})
        assert wrong_ack.status_code == 409
        assert client.get(Q + "/jobs").json()["items"] == []
        accepted = client.post(Q + "/jobs", json=request)
        assert accepted.status_code == 201, accepted.text
        job_id = accepted.json()["job_id"]
        worker = subprocess.run(
            [
                sys.executable,
                "-m",
                "ashare_quant.deployment",
                "--deployment",
                str(config.deployment),
                "task-worker",
                "--task-id",
                job_id,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
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
        assert (
            restarted.get(P + "/config").json()["configuration_id"]
            == configuration.json()["configuration_id"]
        )
        assert restarted.get(Q + "/jobs/" + job_id + "/result").json() == completed
        assert restarted.get(Q + "/catalogue").json()["datasets"] == [descriptor]
    assert not (config.data_root / "daily.sqlite3").exists()
    report_root = os.getenv("ASHARE_QUANT_TEST_REPORT_ROOT")
    if report_root:
        target = Path(report_root).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        (target / f"real-backtest-{start}-{end}.json").write_text(
            json.dumps(
                {
                    "configuration": configuration.json(),
                    "source_query": query,
                    "prices": observed,
                    "raw_source_values_equal": True,
                    "publication": published.json(),
                    "preflight": preflight,
                    "strategy": strategy,
                    "job_request": request,
                    "worker": worker_state,
                    "result": completed,
                    "restart_result_equal": True,
                    "scope": "isolated price-only diagnostic; no live account or historical-order replay",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )


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


@pytest.mark.parametrize("stage", ["catalog_commit", "consumer_read"])
def test_busy_publication_can_retry_exact_request_without_duplicate_state(
    library_configuration, monkeypatch, stage
):
    """Real SQLite contention, not a fabricated OperationalError or replacement price feed."""
    from ashare_data.product.publication import PUBLICATION_SCHEMA

    from ashare_quant.product import publication, service

    config = library_configuration
    catalog = config.release_store_root / "catalog.sqlite"
    with sqlite3.connect(catalog) as db:
        db.executescript(PUBLICATION_SCHEMA)
    causes = []
    original_call = service.Product.call

    def record_cause(self, operation):
        try:
            return original_call(self, operation)
        except Exception as exc:
            causes.append(exc.__cause__)
            raise

    monkeypatch.setattr(service.Product, "call", record_cause)
    original_consume = publication.consume

    def consume_while_catalog_locked(*args, **kwargs):
        # AD has committed its release, but AQ has not yet imported it. Hold a real
        # separate connection's EXCLUSIVE lock for this one read attempt only.
        monkeypatch.setattr(publication, "consume", original_consume)
        with sqlite3.connect(catalog, timeout=0) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                return original_consume(*args, **kwargs)
            finally:
                blocker.rollback()

    request = {
        "securities": SECURITIES,
        "start": "2022-04-06",
        "end": "2022-04-11",
        "source": "auto",
        "mode": "price_only_no_limits",
        "name": "Busy publication retry",
    }
    with TestClient(create_app(product_configuration=config)) as client:
        if stage == "consumer_read":
            monkeypatch.setattr(publication, "consume", consume_while_catalog_locked)
            failed = client.post(P + "/releases", json=request)
        else:
            # A normal reader can overlap the serialized writer's commit. Store's
            # zero-wait SQLite policy returns BUSY instead of extending the request.
            with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as blocker:
                blocker.execute("BEGIN")
                blocker.execute("SELECT version FROM meta").fetchall()
                try:
                    failed = client.post(P + "/releases", json=request)
                finally:
                    blocker.rollback()
        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["code"] == "PRODUCT_DATA_UNAVAILABLE"
        assert failed.headers["Retry-After"] == "2"
        assert failed.headers["Cache-Control"] == "no-store"
        assert len(causes) == 1 and isinstance(causes[0], sqlite3.OperationalError)
        assert causes[0].sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert client.get(Q + "/catalogue").json()["datasets"] == []
        assert client.get(Q + "/jobs").json()["items"] == []
        with sqlite3.connect(catalog) as db:
            expected = 0 if stage == "catalog_commit" else 1
            assert db.execute("SELECT count(*) FROM local_daily_products").fetchone()[0] == expected
        manifests = list(
            (config.release_store_root / "local_daily_products").glob("*/manifest.json")
        )
        assert len(manifests) == 1
        original_bytes = {p.name: p.read_bytes() for p in manifests[0].parent.iterdir()}

        # The lock has now gone; retry the unchanged payload, then repeat it again.
        retry = client.post(P + "/releases", json=request)
        repeat = client.post(P + "/releases", json=request)
        assert retry.status_code == repeat.status_code == 201, retry.text + repeat.text
        assert retry.json() == repeat.json()
        result = retry.json()
        assert result["release"]["dataset_id"] == manifests[0].parent.name
        assert {p.name: p.read_bytes() for p in manifests[0].parent.iterdir()} == original_bytes
        assert client.get(Q + "/catalogue").json()["datasets"] == [result["dataset"]]
        assert client.get(Q + "/jobs").json()["items"] == []
        with sqlite3.connect(catalog) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM local_daily_products").fetchone()[0] == 1
