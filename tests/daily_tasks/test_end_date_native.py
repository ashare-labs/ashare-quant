"""Execute prefixes of supplied owner windows; do not fabricate market facts."""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.storage.store import Store
from ashare_quant.tasks.worker import work_one
from tests.daily_tasks.conftest import request
from tests.daily_tasks.test_native import complete
from tests.daily_tasks.test_python_native import callback_lines
from tests.support.daily.executor import Configuration
from tests.support.daily.service import Jobs
from tests.support.legacy_python.spec import PythonDescriptor

EXAMPLES = Path(__file__).parents[2] / "tests/fixtures/research/python"


def make(jobs, client, *, window="w1", end="2020-01-06", example="staged", key="prefix"):
    version = jobs.register_python(EXAMPLES / (example + ".py"), PythonDescriptor())
    body = request(
        client,
        window=window,
        key=key,
        parameters={
            "template": "registered_python.v1",
            "strategy_version_id": version.version_id,
            "initial_cash": "10000",
            "strategy_parameters": {},
        },
    )
    body["end_date"] = end
    response = client.post("/api/v1/tasks/daily/jobs", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


@pytest.mark.parametrize(
    "window,end,example,cash,equity,quantity,fills,count",
    [
        ("w1", "2020-01-03", "hold", "8735", "9995", 100, 1, 1),
        ("w1", "2020-01-06", "staged", "7484", "9976", 200, 2, 2),
        ("w1", "2020-01-07", "staged", "9976.5", "9976.5", 0, 3, 3),
        ("w2", "2020-01-06", "hold", "8749", "9995", 100, 1, 1),
    ],
)
def test_selected_prefix_matches_account_oracle_and_has_no_tail_events(
    host, window, end, example, cash, equity, quantity, fills, count
):
    jobs, client = host
    catalogue = client.get("/api/v1/tasks/daily/jobs/catalogue").json()
    assert catalogue["end_date_selection"]["start_date_selectable"] is False
    job, body = make(jobs, client, window=window, end=end, example=example)
    assert client.post("/api/v1/tasks/daily/jobs", json=body).json()["job_id"] == job["job_id"]
    assert job["total_cursors"] == count * 3
    result = complete(jobs, job["job_id"])
    assert len(result.days) == count
    assert result.execution_scope.end_date.isoformat() == end
    assert result.execution_scope.requested_end_date.isoformat() == end
    assert result.scope.trading_dates == result.execution_scope.trading_dates
    assert Decimal(result.summary.final_cash) == Decimal(cash)
    assert Decimal(result.summary.final_equity) == Decimal(equity)
    assert result.summary.final_quantity == quantity
    assert result.summary.committed_fill_count == fills
    assert result.days[0].account_at_day_close.sellable == 0
    assert not result.historical_pit_verified and not result.paper_admitted
    assert result.data_identity.source_evidence_kind == "source_claim_unverified"
    manifest = jobs.check(jobs.repo.row(job["job_id"]))
    assert len(manifest.approved_window.scope.trading_dates) == 10
    assert manifest.session["entry"] == jobs.binding().entry(window).model_dump(mode="json")
    assert manifest.session["request"]["end_date"] == end
    assert len(manifest.prepared_proof["records"]) == count * 3
    assert all(record["dt"][:10] <= end for record in manifest.prepared_proof["records"])
    assert all(day.trading_date.isoformat() <= end for day in result.days)
    assert all(read.trading_date.isoformat() <= end for read in result.engine_price_reads)
    root = jobs.directory(job["job_id"])
    assert len(callback_lines(root)) == count
    # Settlement may read the declared next-open metadata after end, never a tail price/bar.
    assert result.execution_scope.settlement_successor > result.execution_scope.end_date
    assert client.get(f"/api/v1/tasks/daily/jobs/{job['job_id']}/result").status_code == 200


@pytest.mark.parametrize("end", ["2020-01-02", "2020-01-04", "2020-01-20"])
def test_unadmitted_or_nontrading_end_refuses_before_job_creation(host, end):
    jobs, client = host
    body = request(client)
    body["end_date"] = end
    before = client.get("/api/v1/tasks/daily/jobs").json()
    response = client.post("/api/v1/tasks/daily/jobs", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "M2_END_DATE_NOT_ADMITTED"
    assert client.get("/api/v1/tasks/daily/jobs").json() == before
    assert not list(jobs.root.glob("m2job-*"))


def test_end_date_is_immutable_and_cannot_extend_an_accepted_job(host):
    jobs, client = host
    job, body = make(jobs, client)
    changed = body | {"end_date": "2020-01-07"}
    response = client.post("/api/v1/tasks/daily/jobs", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "M2_JOB_IDEMPOTENCY_CONFLICT"
    file = jobs.directory(job["job_id"]) / "execution-scope.json"
    raw = file.read_bytes()
    try:
        value = json.loads(raw)
        value["end_date"] = "2020-01-07"
        file.write_text(json.dumps(value))
        with pytest.raises(DomainError) as exc:
            jobs.get(job["job_id"])
        assert exc.value.code == "M2_EXECUTION_SCOPE_CHANGED"
    finally:
        file.write_bytes(raw)
    assert jobs.get(job["job_id"]).request.end_date.isoformat() == "2020-01-06"


def test_prefix_native_restart_and_relocation_never_resume_parent_tail(host, tmp_path):
    jobs, client = host
    job, _ = make(jobs, client)
    work_one(jobs, job["job_id"], _fault="native_submit:5")
    assert jobs.get(job["job_id"]).status == "failed"
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(str(jobs.repo.store.path), moved / "prefix.sqlite3")
    shutil.move(str(jobs.root), moved / "prefix.sqlite3.tasks")
    shutil.copytree(jobs.config.owner_store, moved / "owner")
    resumed = Jobs(Store(moved / "prefix.sqlite3"), Configuration(moved / "owner"))
    resumed.resume(job["job_id"])
    result = complete(resumed, job["job_id"])
    assert len(result.days) == 2 and result.summary.committed_fill_count == 2
    assert Decimal(result.summary.final_cash) == Decimal("7484")
    assert result.summary.final_quantity == 200
    assert resumed.get(job["job_id"]).total_cursors == 6
    assert len(callback_lines(resumed.directory(job["job_id"]))) == 2
    assert len({fill.fill_id for day in result.days for fill in day.fills}) == 2


def test_prefix_completion_reentry_does_not_continue_full_window(host):
    jobs, client = host
    job, _ = make(jobs, client, end="2020-01-03", example="hold")

    def stage(name, *args):
        if name == "before_publish":
            raise RuntimeError("controlled publication interruption")

    work_one(jobs, job["job_id"], _stage=stage)
    jobs.resume(job["job_id"])
    result = complete(jobs, job["job_id"])
    assert len(result.days) == 1
    assert result.recovery["completion"]["native_run"] is False
    assert len(callback_lines(jobs.directory(job["job_id"]))) == 1


def test_omitted_end_and_explicit_last_day_keep_whole_window_economics(host):
    jobs, client = host
    implicit, _ = make(jobs, client, window="two_day", end=None, example="hold", key="implicit")
    explicit, _ = make(jobs, client, window="two_day", end="2020-01-06", example="hold", key="explicit")
    a = complete(jobs, implicit["job_id"])
    b = complete(jobs, explicit["job_id"])
    assert a.summary == b.summary and a.account == b.account
    assert a.scope.trading_dates == b.scope.trading_dates
    assert a.execution_scope.requested_end_date is None
    assert b.execution_scope.requested_end_date.isoformat() == "2020-01-06"
