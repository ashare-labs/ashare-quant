"""Acceptance on a supplied, closed historical database copy; no task execution."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app


def inventory(root):
    return {
        str(p.relative_to(root)): [
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
            p.stat().st_size,
        ]
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("version", [3, 4, 5])
def test_closed_historical_database_copy_preserves_all_task_files(tmp_path, version):
    supplied = os.getenv(f"ASHARE_QUANT_TEST_V{version}_DATABASE")
    if not supplied:
        pytest.skip(f"Supply a closed historical v{version} database copy; not executed")
    source = Path(supplied)
    assert not Path(str(source) + "-wal").exists(), "Close the old service before making a copy"
    source_before = inventory(source.parent)
    root = tmp_path / "historical"
    root.mkdir()
    path = root / source.name
    shutil.copy2(source, path)
    task_root = Path(str(source) + ".m2-jobs")
    assert task_root.is_dir(), "Provide the historical task directory beside the database"
    shutil.copytree(task_root, Path(str(path) + ".m2-jobs"))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        rows = db.execute("SELECT job_id,status,manifest FROM m2_jobs ORDER BY job_id").fetchall()
        assert rows
        assert {json.loads(row[2])["schema_version"] for row in rows} == {
            f"m2-job-manifest.v{version}"
        }
    before = inventory(root)
    responses = {}
    with TestClient(create_app(str(path))) as client:
        for url in ["/api/v1/m2/jobs", *(f"/api/v1/m2/jobs/{row[0]}" for row in rows)]:
            response = client.get(url)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "M2_DATABASE_VERSION_INCOMPATIBLE"
            responses[url] = response.json()
    cli = subprocess.run(
        [sys.executable, "-m", "ashare_quant.cli", "--db", str(path), "job-worker", "--once"],
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 2, cli.stdout + cli.stderr
    assert json.loads(cli.stdout)["error"]["code"] == "M2_DATABASE_VERSION_INCOMPATIBLE"
    assert inventory(root) == before
    assert inventory(source.parent) == source_before
    (tmp_path / "compatibility-evidence.json").write_text(
        json.dumps(
            {
                "historical_job_ids_and_statuses": [row[:2] for row in rows],
                "responses": responses,
                "cli": json.loads(cli.stdout),
                "files_unchanged": True,
                "historical_files": before,
                "supplied_copy_unchanged": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
