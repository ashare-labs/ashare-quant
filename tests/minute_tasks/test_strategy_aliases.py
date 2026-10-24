"""One filesystem slot has one new-run identity; frozen historical IDs stay exact."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ashare_quant.contracts import DomainError
from ashare_quant.strategies.files import StrategyFiles
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import fixed_release as fixed_release


@pytest.mark.parametrize("saved,alias", [
    ("Research/Case.py", "research/case.py"),
    ("Caf\u00e9.py", "Cafe\u0301.py"),
])
def test_file_alias_responses_use_actual_names_without_renaming(tmp_path, saved, alias):
    files = StrategyFiles(tmp_path)
    if "/" in saved:
        files.mkdir(saved.rsplit("/", 1)[0])
    files.save(saved, "original")
    if not (files.root / alias).exists():
        pytest.skip("This filesystem does not equate these names")
    parent = saved.rsplit("/", 1)[0] if "/" in saved else ""
    canonical = files.list(parent)["entries"][0]["path"]
    assert files.read(alias) == {"path": canonical, "strategy_id": canonical, "source": "original"}
    with pytest.raises(DomainError, match="File exists"):
        files.save(alias, "must not overwrite")
    assert files.save(alias, "revised", overwrite=True) == {"path": canonical, "strategy_id": canonical}
    assert files.list(parent)["entries"][0]["path"] == canonical
    assert files.read(saved)["source"] == "revised"
    if parent:
        assert files.mkdir(parent.lower())["path"] == parent
        listing = files.list(parent.lower())
        assert listing["path"] == parent and listing["entries"][0]["strategy_id"] == canonical


def test_distinct_case_sensitive_slots_are_not_merged(tmp_path):
    files = StrategyFiles(tmp_path)
    files.save("Case.py", "first")
    if (files.root / "case.py").exists():
        pytest.skip("Requires a case-sensitive filesystem")
    files.save("case.py", "second")
    assert files.read("Case.py")["strategy_id"] == "Case.py"
    assert files.read("case.py")["strategy_id"] == "case.py"
    assert len(files.list()["entries"]) == 2


def test_alias_submission_freezes_canonical_id_and_retries_without_live_file(daily_jobs, tmp_path):
    app, jobs, body = daily_jobs
    jobs.product.configured = lambda: SimpleNamespace(control_db=tmp_path / "control.sqlite3")
    files = StrategyFiles(tmp_path)
    files.save("Case.py", "def initialize(context): pass\n")
    if not (files.root / "case.py").exists():
        pytest.skip("Requires the deployed Mac's case-insensitive filesystem")
    body.update(strategy_id="case.py", end_date=body["start_date"], source="def initialize(c): pass\n")
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["request"]["strategy_id"] == "Case.py"
        manifest_path = jobs.directory(created["job_id"]) / "accepted-manifest.json"
        frozen = manifest_path.read_bytes()
        assert client.get("/api/v1/python/backtests", params={"strategy_id": "Case.py"}).json()["total"] == 1
        assert client.get("/api/v1/python/backtests", params={"strategy_id": "case.py"}).json()["total"] == 0
        # A retry is about immutable acceptance, not today's file existence or bytes.
        (files.root / "Case.py").unlink()
        for retry in (body, {**body, "strategy_id": "Case.py"}):
            again = client.post("/api/v1/python/jobs", json=retry)
            assert again.status_code == 201 and again.json()["job_id"] == created["job_id"]
        for wrong in (None, "elsewhere.py", "CASE.py"):
            conflict = client.post("/api/v1/python/jobs", json={**body, "strategy_id": wrong})
            assert conflict.status_code == 409
        assert manifest_path.read_bytes() == frozen and len(jobs.repo.rows()) == 1


def test_canonical_acceptance_does_not_let_retry_drop_identity(daily_jobs, tmp_path):
    app, jobs, body = daily_jobs
    jobs.product.configured = lambda: SimpleNamespace(control_db=tmp_path / "control.sqlite3")
    files = StrategyFiles(tmp_path)
    files.save("one.py", "")
    body.update(strategy_id="one.py", end_date=body["start_date"])
    with TestClient(app) as client:
        assert client.post("/api/v1/python/jobs", json=body).status_code == 201
        assert client.post("/api/v1/python/jobs", json={**body, "strategy_id": None}).status_code == 409


def test_legacy_alias_is_not_rewritten_or_guessed_into_new_identity(daily_jobs, tmp_path, monkeypatch):
    app, jobs, body = daily_jobs
    jobs.product.configured = lambda: SimpleNamespace(control_db=tmp_path / "control.sqlite3")
    files = StrategyFiles(tmp_path)
    files.save("Case.py", "")
    if not (files.root / "case.py").exists():
        pytest.skip("Requires a case-insensitive filesystem")
    body.update(strategy_id="case.py", end_date=body["start_date"])
    actual_read = StrategyFiles.read

    def legacy_read(self, path):
        return {**actual_read(self, path), "path": path, "strategy_id": path}

    with TestClient(app) as client:
        # Construct an accepted record with the old exact-path contract.
        with monkeypatch.context() as patch:
            patch.setattr(StrategyFiles, "read", legacy_read)
            old = client.post("/api/v1/python/jobs", json=body).json()
        saved = jobs.directory(old["job_id"]) / "accepted-manifest.json"
        frozen = saved.read_bytes()
        new = client.post("/api/v1/python/jobs", json={**body, "idempotency_key": "new"}).json()
        assert new["request"]["strategy_id"] == "Case.py"
        assert client.post("/api/v1/python/jobs", json=body).json()["job_id"] == old["job_id"]
        assert client.get("/api/v1/python/backtests", params={"strategy_id": "case.py"}).json()["items"][0]["job_id"] == old["job_id"]
        assert client.get("/api/v1/python/backtests", params={"strategy_id": "Case.py"}).json()["items"][0]["job_id"] == new["job_id"]
        assert saved.read_bytes() == frozen
