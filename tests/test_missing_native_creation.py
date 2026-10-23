"""AQ-DATE-01: missing native runtime must not create invisible task artifacts."""

import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from tests.support.app import create_app
from tests.support.daily.executor import Configuration


def test_missing_native_retries_preserve_existing_residue_and_create_nothing(tmp_path, monkeypatch):
    from ashare_data import Store as DataStore

    import tests.support.daily.executor as executor

    product = os.getenv("ASHARE_QUANT_TEST_M2_PRODUCT")
    if not product:
        pytest.skip("Requires explicit existing public owner export; no fabricated market inputs")
    owner_root = tmp_path / "owner"
    owner = DataStore.init(owner_root)
    dataset = owner.import_portable(Path(product))
    native_absent = importlib.util.find_spec("rqalpha") is None
    if not native_absent:
        def missing_runtime(*, native=False):
            assert native
            raise DomainError("RUNTIME_DEPENDENCY_MISMATCH", "Controlled missing-native failure")
        monkeypatch.setattr(executor, "runtime_fingerprint", missing_runtime)
    app = create_app(str(tmp_path / "control.sqlite3"), daily_tasks_configuration=Configuration(owner_root))
    with TestClient(app) as client:
        assert client.post("/api/v1/tasks/daily/data/bind", json={"dataset_id": dataset}).status_code == 200
        catalogue = client.get("/api/v1/tasks/daily/jobs/catalogue").json()
        window = next(w for w in catalogue["windows"] if w["scope"]["window_id"] == "w2")
        request = {"window_id": "w2", "end_date": "2020-01-07", "name": "Missing native retry",
                   "idempotency_key": "same-key", "data_binding_sha256": catalogue["data_binding_sha256"],
                   "assumption_ack_sha256": window["assumption_ack_sha256"], "parameters": catalogue["parameters"]}
        jobs = app.state.daily_tasks
        # An unrelated prior residual is preserved; this fix performs no cleanup.
        residue = jobs.root / ("m2job-" + "0" * 32)
        residue.mkdir()
        (residue / "legacy.txt").write_bytes(b"preserve previous residual\n")
        before = jobs.list_strategies()
        responses = []
        for _ in range(3):
            response = client.post("/api/v1/tasks/daily/jobs", json=request)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "RUNTIME_DEPENDENCY_MISMATCH"
            assert list(jobs.root.glob("m2job-*")) == [residue]
            assert (residue / "legacy.txt").read_bytes() == b"preserve previous residual\n"
            assert client.get("/api/v1/tasks/daily/jobs").json() == {"items": []}
            assert jobs.list_strategies() == before
            responses.append(response.json())
        (tmp_path / "missing-native-evidence.json").write_text(json.dumps({
            "real_native_absent": native_absent, "native_failure_injected": not native_absent,
            "attempts": 3, "new_task_directories": 0, "new_strategies": 0,
            "existing_residue_unchanged": True, "responses": responses,
        }, indent=2) + "\n")
