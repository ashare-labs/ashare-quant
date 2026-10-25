"""Frozen multifile snapshots through real AD fixtures, HTTP jobs and native RQAlpha.

The release contains constructed prices, not production market data or CMES data.
"""

import json

import pytest
from fastapi.testclient import TestClient

from ashare_quant.storage.artifacts import digest
from ashare_quant.strategies.projects import ProjectFiles
from ashare_quant.tasks.worker import work_one
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release

PROJECT_SOURCES = {
    "main.py": '''from .lifecycle import initialize, on_strategy_end
assert __spec__.name == __name__
assert __package__ == __name__.rsplit('.', 1)[0]
assert __file__.endswith('/source/main.py')
''',
    "lifecycle.py": '''from aquant import g, run_daily, log
from .signals.rules import rebalance
def initialize(context):
    g.context_id = id(context)
    run_daily(rebalance, 'open')
def on_strategy_end(context):
    assert g.context_id == id(context)
    log.info('project-finished')
''',
    "signals/__init__.py": "from .rules import rebalance\n",
    "signals/rules.py": '''from aquant import attribute_history, g, order_target, record
from ..settings import SHARES
from .. import math as local_math
from ..risk.policy import allowed
import math
def rebalance(context):
    assert g.context_id == id(context)
    assert allowed and local_math.answer == 7 and math.sqrt(9) == 3
    sid = context.securities[0]
    values = attribute_history(sid, 5, fields=['close'], fq=None)
    assert len(values) == 5
    order_target(sid, SHARES)
    record(target=SHARES)
''',
    "settings.py": "SHARES = 100\n",
    "math.py": "answer = 7\n",
    "risk/policy.py": "allowed = True\n",
}


def project_submission(app, jobs, body, *, name="多文件策略", files=None):
    configured = jobs.product.configured()
    configured.control_db = app.state.store.path
    jobs.product.configured = lambda: configured
    saved = ProjectFiles(configured.control_db.parent).create(name)
    submission = {key: value for key, value in body.items() if key != "source"}
    submission.update(
        strategy_id=saved["strategy_id"], end_date=body["start_date"],
        project={"project_id": saved["strategy_id"], "entrypoint": "main.py", "files": files or dict(PROJECT_SOURCES)},
    )
    return submission, saved


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
def test_project_uses_frozen_helpers_real_history_trades_and_changed_identity(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body["frequency"] = frequency
    submission, saved = project_submission(app, jobs, body)
    with TestClient(app) as client:
        first = execute(client, jobs, submission)
        assert first["status"] == "succeeded", first.get("error")
        first_result = client.get(first["result"]["href"]).json()
        assert first_result["native"]["real_strategy_executed"]
        assert first_result["summary"]["trade_count"] == 1
        assert first_result["native"]["trades"][0]["amount"] == 100
        assert first_result["native"]["trades"][0]["at"][11:16] == ("09:30" if frequency == "1d" else "09:31")
        assert first_result["logs"][-1]["message"] == "project-finished"
        root = jobs.directory(first["job_id"])
        frozen = json.loads((root / "accepted-manifest.json").read_text())
        session = json.loads((root / "session.json").read_text())
        assert frozen["schema_version"] == "python-project-job.v3"
        assert session["project_sha256"] == digest(submission["project"])
        assert {name for name in frozen["files"] if name.startswith("source/")} == {
            "source/" + name for name in PROJECT_SOURCES
        }
        for name, source in PROJECT_SOURCES.items():
            assert (root / "source" / name).read_text() == source
        assert not (root / "strategy.py").exists()
        assert json.loads((root / "teardown.json").read_text())["native_singleton_cleared"]
        changed_project = submission["project"] | {"files": PROJECT_SOURCES | {"settings.py": "SHARES = 200\n"}}
        second = execute(client, jobs, submission | {"idempotency_key": "changed-helper", "project": changed_project})
        assert second["status"] == "succeeded", second.get("error")
        second_result = client.get(second["result"]["href"]).json()
        assert second_result["native"]["trades"][0]["amount"] == 200
        assert second_result["native"]["identity"] != first_result["native"]["identity"]
        changed_session = json.loads((jobs.directory(second["job_id"]) / "session.json").read_text())
        assert changed_session["project_sha256"] != session["project_sha256"]
        assert changed_session["request_sha256"] != session["request_sha256"]
        assert (root / "source/settings.py").read_text() == "SHARES = 100\n"
        assert client.get(first["result"]["href"]).json() == first_result
        # All submitted edits were frozen together; running did not save a draft.
        assert ProjectFiles(app.state.store.path.parent).read_id(saved["strategy_id"])["files"] == saved["files"]
        other_submission, other_saved = project_submission(
            app, jobs, body, name="另一个项目",
            files=PROJECT_SOURCES | {"settings.py": "SHARES = 300\n"},
        )
        assert other_saved["strategy_id"] != saved["strategy_id"]
        other = execute(client, jobs, other_submission | {"idempotency_key": "different-project"})
        assert other["status"] == "succeeded", other.get("error")
        other_result = client.get(other["result"]["href"]).json()
        assert other_result["native"]["trades"][0]["amount"] == 300
        assert client.get(first["result"]["href"]).json() == first_result


def test_helper_error_exposes_relative_filename_and_line(daily_jobs):
    app, jobs, body = daily_jobs
    submission, _ = project_submission(app, jobs, body, files={
        "main.py": "from .nested.callbacks import initialize, handle_data\n",
        "nested/callbacks.py": "def initialize(context): pass\ndef handle_data(context, data):\n    raise ValueError('helper-boom')\n",
    })
    with TestClient(app) as client:
        job = execute(client, jobs, submission)
        assert job["status"] == "failed", job
        assert job["error"]["source_file"] == "nested/callbacks.py"
        assert job["error"]["source_line"] == 3
        assert "nested/callbacks.py 第 3 行" in job["error"]["message"]
        assert client.get("/api/v1/python/jobs/" + job["job_id"] + "/result").status_code == 409


def test_legacy_import_in_any_project_file_is_rejected_before_queue(daily_jobs):
    app, jobs, body = daily_jobs
    submission, _ = project_submission(app, jobs, body, files={
        "main.py": "def initialize(context): pass\n",
        "unused.py": "import jqdata\n",
    })
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=submission)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "AQUANT_LEGACY_IMPORT"
        assert response.json()["error"]["details"]["source_file"] == "unused.py"
        assert client.get("/api/v1/python/jobs").json()["items"] == []


def test_execution_cannot_publish_after_an_unimported_frozen_file_changes(daily_jobs):
    app, jobs, body = daily_jobs
    submission, _ = project_submission(app, jobs, body, files={
        "main.py": "from pathlib import Path\ndef initialize(context):\n    Path(__file__).with_name('unused.py').write_text('changed = True\\n')\n",
        "unused.py": "original = True\n",
    })
    with TestClient(app) as client:
        created = client.post("/api/v1/python/jobs", json=submission)
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]
        assert work_one(jobs, job_id) == job_id
        assert jobs.repo.row(job_id)["status"] == "failed"
        root = jobs.directory(job_id)
        errors = [json.loads(path.read_text()) for path in root.glob("*.runner-error.json")]
        assert any(error["code"] == "PYTHON_PROJECT_CHANGED" for error in errors)
        assert not (root / "completed.json").exists()
        assert not (root / "result.json").exists()
        # Integrity failure also makes historical readback fail closed.
        response = client.get("/api/v1/python/jobs/" + job_id + "/result")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PYTHON_INPUT_CHANGED"
