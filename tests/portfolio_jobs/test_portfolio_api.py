"""Real exported data only; unavailable external evidence is explicitly skipped."""

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.m2_jobs.files import sha
from ashare_quant.m2_jobs.worker import work_one
from ashare_quant.portfolio_jobs.contracts import PortfolioSelection

P = "/api/v1/portfolio"


@pytest.fixture
def host(tmp_path):
    folder = os.getenv("ASHARE_QUANT_TEST_PORTFOLIO_EXPORTS")
    if not folder:
        pytest.skip("Real portfolio acceptance requires caller-supplied AD exports")
    app = create_app(str(tmp_path / "control.sqlite3"))
    jobs = app.state.portfolio_jobs
    descriptor = jobs.import_data(Path(folder) / "w2020-model.json", "real-2020")
    client = TestClient(app)
    selection = dict(
        dataset_id=descriptor.dataset_id,
        securities=["000001.XSHE", "600036.XSHG"],
        start_date="2020-02-03",
        end_date="2020-02-07",
        accept_retrospective_state=True,
    )
    report = client.post(P + "/preflight", json=selection)
    assert report.status_code == 200 and report.json()["ready"], report.text
    version = client.post(P + "/strategies", json={"algorithm": "equal-weight"}).json()
    request = selection | dict(
        name="Real API test",
        idempotency_key="first",
        strategy_version_id=version["version_id"],
        assumption_ack_sha256=report.json()["assumption_ack_sha256"],
    )
    yield app, jobs, client, request
    client.close()


def create(host, **changes):
    response = host[2].post(P + "/jobs", json=host[3] | changes)
    assert response.status_code == 201, response.text
    return response.json()


def finish(host, job):
    app, jobs, client, _ = host
    assert work_one(app.state.m2_jobs, job["job_id"]) == job["job_id"]
    state = client.get(P + "/jobs/" + job["job_id"])
    assert state.status_code == 200 and state.json()["status"] == "succeeded", state.text
    result = client.get(P + "/jobs/" + job["job_id"] + "/result")
    assert result.status_code == 200 and result.headers["ETag"], result.text
    return result.json()


def test_http_matches_standalone_and_reopens(host, tmp_path):
    app, jobs, client, request = host
    job = create(host)
    result = finish(host, job)
    assert len(result["days"]) == 5 and result["audit"]["passed"]
    assert {t["security"] for t in result["trades"]} == set(request["securities"])
    assert result["scope"]["timezone"] == "Asia/Shanghai"
    assert all(
        datetime.fromisoformat(t["at"]).utcoffset().total_seconds() == 8 * 3600
        for t in result["trades"] + result["orders"] + result["days"]
    )
    assert sum(Decimal(day["day_fee"]) for day in result["days"]) == Decimal(
        result["summary"]["fee_total"]
    )
    assert result["days"][-1]["cumulative_fee"] == result["summary"]["fee_total"]
    assert result["account"]["at"] is None
    assert result["summary"]["benchmark"] is None and result["summary"]["sharpe"] is None
    assert datetime.fromisoformat(result["receipt"]["trades"][0]["at"]).tzinfo is None
    root = jobs.directory(job["job_id"])
    before = sha((root / "native.chk").read_bytes())
    assert create(host) == client.get(P + "/jobs/" + job["job_id"]).json()
    assert work_one(app.state.m2_jobs, job["job_id"]) is None
    assert sha((root / "native.chk").read_bytes()) == before
    direct = tmp_path / "standalone"
    command = [
        sys.executable,
        "-B",
        "-m",
        "ashare_quant.daily_portfolio",
        "--manifest",
        str(root / "execution.json"),
        "--input",
        str(root / "input.json"),
        "--strategy",
        "equal-weight",
        "--window",
        "selected",
        "--output",
        str(direct),
        "--accept-owner-state-model",
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads((direct / "result.json").read_text()) == result["receipt"]
    with TestClient(create_app(str(jobs.repo.store.path))) as reopened:
        assert reopened.get(P + "/jobs/" + job["job_id"] + "/result").json() == result
        assert reopened.get("/api/v1/m2/jobs").json() == {"items": []}
        assert reopened.get("/api/v1/m2/strategies").json() == []
        assert reopened.get("/api/v1/m2/python-strategies").json() == []


def test_concurrent_idempotency_and_account_isolation(host):
    with ThreadPoolExecutor(max_workers=3) as pool:
        duplicates = list(pool.map(lambda _: create(host), range(3)))
    assert len({j["job_id"] for j in duplicates}) == 1
    conflict = host[2].post(P + "/jobs", json=host[3] | {"initial_cash": "2000000"})
    assert conflict.status_code == 409
    other = create(host, idempotency_key="second")
    assert other["account_id"] != duplicates[0]["account_id"]
    assert other["session_sha256"] != duplicates[0]["session_sha256"]
    one, two = finish(host, duplicates[0]), finish(host, other)
    assert one["summary"] == two["summary"] and one["trades"] == two["trades"]
    assert one["receipt"]["identity"] != two["receipt"]["identity"]
    assert len(host[1].repo.rows()) == 2


@pytest.mark.parametrize(
    "fault", ["before_replace:2", "after_replace:3", "native_submit:2", "decision_candidate:4"]
)
def test_atomic_crash_resume_matches_baseline(host, fault):
    app, jobs, client, _ = host
    baseline = finish(host, create(host))
    interrupted = create(host, idempotency_key="crash")
    jid = interrupted["job_id"]
    work_one(app.state.m2_jobs, jid, _fault=fault)
    failed = client.get(P + "/jobs/" + jid).json()
    assert failed["status"] == "failed" and failed["can_resume"] and failed["result"] is None
    assert client.get(P + "/jobs/" + jid + "/result").status_code == 409
    assert client.post(P + "/jobs/" + jid + "/resume", json={}).json()["status"] == "queued"
    recovered = finish(host, interrupted)
    for key in ("days", "orders", "trades", "decisions", "summary", "account"):
        assert recovered[key] == baseline[key], key
    assert recovered["account_id"] == interrupted["account_id"]
    assert jobs.get(jid).attempt_count == 2


@pytest.mark.parametrize("stage", ["queued", "spawned", "after_publish"])
def test_cancel_resume_never_publishes_cancelled_success(host, stage):
    app, jobs, client, _ = host
    job = create(host)
    jid = job["job_id"]
    if stage == "queued":
        client.post(P + "/jobs/" + jid + "/cancel", json={})
    else:

        def cancel(name, *_):
            if name == stage:
                jobs.cancel(jid)

        work_one(app.state.m2_jobs, jid, _stage=cancel)
    view = jobs.get(jid)
    assert view.status == "cancelled" and view.result is None
    assert client.get(P + "/jobs/" + jid + "/result").status_code == 409
    checkpoint = jobs.directory(jid) / "native.chk"
    before = checkpoint.read_bytes() if stage == "after_publish" else None
    jobs.resume(jid)
    result = finish(host, job)
    assert result["account_id"] == job["account_id"]
    assert len({t["id"] for t in result["trades"]}) == len(result["trades"])
    if before:
        assert checkpoint.read_bytes() == before


def test_tampering_and_strategy_versions_fail_closed(host):
    _, jobs, client, request = host
    version = client.post(P + "/strategies", json={"algorithm": "trend"}).json()
    assert version["version_id"] != request["strategy_version_id"]
    job = create(host)
    path = jobs.directory(job["job_id"]) / "input.json"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    assert client.get(P + "/jobs/" + job["job_id"]).status_code == 409
    path.write_bytes(original)
    result = finish(host, job)
    assert result["strategy"]["version_id"] == request["strategy_version_id"]
    published = (
        jobs.directory(job["job_id"]) / jobs.get(job["job_id"]).result.publication / "receipt.json"
    )
    published.write_text("{}")
    assert client.get(P + "/jobs/" + job["job_id"] + "/result").status_code == 409
    assert client.get(P + "/jobs/" + job["job_id"]).status_code == 409


def test_preflight_gaps_and_strict_contract(host):
    _, jobs, client, request = host
    selection = {k: request[k] for k in PortfolioSelection.model_fields if k in request}
    for changes in (
        {"securities": ["600999.XSHG"]},
        {"start_date": "2019-02-01"},
        {"end_date": "2020-02-08"},
        {"accept_retrospective_state": False},
    ):
        response = client.post(P + "/preflight", json=selection | changes)
        assert (
            response.status_code == 200 and not response.json()["ready"] and response.json()["gaps"]
        )
        refused = client.post(P + "/jobs", json=request | changes)
        assert (
            refused.status_code == 422 and refused.json()["error"]["code"] == "PORTFOLIO_DATA_GAPS"
        )
    for changes in (
        {"fee_model": "free"},
        {"account_id": "paper-today"},
        {"frequency": "1m"},
        {"parameters": {}},
        {"volume_ratio": "1.2"},
        {"initial_cash": "0"},
    ):
        assert client.post(P + "/jobs", json=request | changes).status_code == 422
    bad_ack = client.post(P + "/jobs", json=request | {"assumption_ack_sha256": "0" * 64})
    assert bad_ack.status_code == 409 and jobs.repo.rows() == []


def test_future_unsubmitted_tail_cannot_change_earlier_results(host, tmp_path):
    _, jobs, client, request = host
    original = finish(host, create(host))
    _, document = jobs.datasets.read(request["dataset_id"])
    for row in document["daily"]:
        if row["date"] > request["end_date"]:
            row.update(open="1000", high="1000", low="1000", close="1000")
    path = tmp_path / "changed-future.json"
    path.write_text(json.dumps(document))
    descriptor = jobs.import_data(path, "perturbed future tail for causal test")
    selection = {k: request[k] for k in PortfolioSelection.model_fields if k in request} | {
        "dataset_id": descriptor.dataset_id
    }
    report = client.post(P + "/preflight", json=selection).json()
    assert report["ready"], report
    variant = create(
        host,
        dataset_id=descriptor.dataset_id,
        idempotency_key="future",
        assumption_ack_sha256=report["assumption_ack_sha256"],
    )
    result = finish(host, variant)
    for key in ("days", "trades", "orders", "decisions", "summary"):
        assert result[key] == original[key]


def test_cross_year_without_fake_limits(host):
    _, jobs, client, _ = host
    path = os.getenv("ASHARE_QUANT_TEST_PORTFOLIO_LONG")
    if not path:
        pytest.skip("Cross-year acceptance requires a caller-supplied long AD export")
    descriptor = jobs.import_data(Path(path), "cross-year")
    dates = descriptor.trading_dates
    assert dates[0].year != dates[-1].year
    selection = dict(
        dataset_id=descriptor.dataset_id,
        securities=["000001.XSHE", "000651.XSHE", "600036.XSHG"],
        start_date=str(dates[0]),
        end_date=str(dates[-1]),
        accept_retrospective_state=True,
    )
    covered = client.post(P + "/preflight", json=selection).json()
    assert not covered["ready"]
    selection["mode"] = "price_only_no_limits"
    report = client.post(P + "/preflight", json=selection).json()
    assert report["ready"], report
    version = client.post(P + "/strategies", json={"algorithm": "momentum-low-volatility"}).json()
    job = create(
        host,
        **selection,
        strategy_version_id=version["version_id"],
        assumption_ack_sha256=report["assumption_ack_sha256"],
    )
    result = finish(host, job)
    assert result["economic_scope"] == "price_only_no_limits_not_total_return"
    assert result["receipt"]["price_limit_policy"] == "disabled_for_price_only_diagnostic"
    assert len(result["days"]) == len(dates) and result["audit"]["passed"]
    frozen = json.loads((jobs.directory(job["job_id"]) / "input.json").read_text())
    assert all(r["upper_limit"] is None and r["lower_limit"] is None for r in frozen["daily"])
    assert frozen["actions"] == [] and frozen["action_coverage"]["status"] == "unavailable"


def test_empty_service_and_origin(tmp_path):
    client = TestClient(create_app(str(tmp_path / "control.sqlite3")))
    catalogue = client.get(P + "/catalogue")
    assert catalogue.status_code == 200 and catalogue.json()["datasets"] == []
    assert catalogue.headers["Cache-Control"] == "no-store"
    response = client.post(
        P + "/strategies", json={"algorithm": "trend"}, headers={"Origin": "https://evil.invalid"}
    )
    assert response.status_code == 403
    assert client.post(P + "/strategies", json={"algorithm": "unknown"}).status_code == 422
    assert client.get(P + "/jobs?take=1").status_code == 422
    assert client.get(P + "/openapi.json").status_code == 200
    old = client.get("/api/v1/m2/openapi.json").json()
    assert not any("Portfolio" in name for name in old["components"]["schemas"])


@pytest.mark.parametrize(
    "window,ids,start,end,algorithm",
    [
        (
            "w2022",
            ["000651.XSHE", "000333.XSHE", "601318.XSHG"],
            "2022-04-01",
            "2022-04-15",
            "trend",
        ),
        (
            "w2024",
            ["000001.XSHE", "600036.XSHG", "600519.XSHG"],
            "2024-01-02",
            "2024-01-12",
            "momentum",
        ),
    ],
)
def test_other_real_windows(host, window, ids, start, end, algorithm):
    _, jobs, client, _ = host
    descriptor = jobs.import_data(
        Path(os.environ["ASHARE_QUANT_TEST_PORTFOLIO_EXPORTS"]) / (window + "-model.json"), window
    )
    selection = dict(
        dataset_id=descriptor.dataset_id,
        securities=ids,
        start_date=start,
        end_date=end,
        accept_retrospective_state=True,
    )
    report = client.post(P + "/preflight", json=selection).json()
    assert report["ready"], report
    version = client.post(P + "/strategies", json={"algorithm": algorithm}).json()
    job = create(
        host,
        **selection,
        strategy_version_id=version["version_id"],
        assumption_ack_sha256=report["assumption_ack_sha256"],
    )
    result = finish(host, job)
    assert result["days"][0]["date"] == start and result["days"][-1]["date"] == end
    assert result["scope"]["securities"] == sorted(ids) and result["audit"]["passed"]
    if algorithm == "trend":
        # None of these real observations passes the registered trend rule.
        assert result["trades"] == [] and result["orders"] == []
        assert all(not d["signal"]["selected"] for d in result["decisions"] if d["signal"])
        assert Decimal(result["summary"]["final_equity"]) == Decimal("1000000")
        assert Decimal(result["summary"]["fee_total"]) == 0
    else:
        assert result["trades"]


def test_data_gaps_are_fields_and_never_create_accounts(host, tmp_path):
    _, jobs, client, request = host
    _, document = jobs.datasets.read(request["dataset_id"])
    row = next(
        r
        for r in document["daily"]
        if r["security"] == "600036.XSHG" and r["date"] == request["start_date"]
    )
    row["suspended"] = None
    document["action_coverage"]["status"] = "unavailable"
    path = tmp_path / "missing-state-actions.json"
    path.write_text(json.dumps(document))
    descriptor = jobs.import_data(path, "missing evidence fixture")
    selection = {k: request[k] for k in PortfolioSelection.model_fields if k in request} | {
        "dataset_id": descriptor.dataset_id
    }
    report = client.post(P + "/preflight", json=selection).json()
    assert not report["ready"]
    assert {"suspended", "corporate_actions"} <= {g["field"] for g in report["gaps"]}
    rejected = client.post(P + "/jobs", json=request | selection)
    assert rejected.status_code == 422 and not jobs.repo.rows()


def test_v6_and_portfolio_share_original_worker_and_capacity(host, tmp_path):
    from ashare_data import Store as OwnerStore

    from ashare_quant.m2_jobs.executor import Configuration

    folder = os.getenv("ASHARE_QUANT_TEST_M2_COMPONENTS")
    if not folder:
        pytest.skip("Mixed queue requires caller-supplied M2 owner components")
    app, portfolio, client, _ = host
    owner = OwnerStore.init(tmp_path / "owner")
    market = owner.import_m2_component(Path(folder) / "market")
    facts = owner.import_m2_component(Path(folder) / "facts")
    product = owner.compose_m2(
        price_dataset_id=market,
        facts_component_id=facts,
        window_ids=["m2a"],
        mode="conditional_research",
    ).to_dict()
    app = create_app(
        str(portfolio.repo.store.path), m2_jobs_configuration=Configuration(tmp_path / "owner")
    )
    with TestClient(app) as combined:
        catalogue = combined.post(
            "/api/v1/m2/data/bind", json={"dataset_id": product["dataset_id"]}
        ).json()
        window = catalogue["windows"][0]
        old_request = dict(
            name="Legacy v6 job",
            idempotency_key="legacy",
            window_id="m2a",
            assumption_ack_sha256=window["assumption_ack_sha256"],
            data_binding_sha256=catalogue["data_binding_sha256"],
            parameters=catalogue["parameters"],
        )
        response = combined.post("/api/v1/m2/jobs", json=old_request)
        assert response.status_code == 201, response.text
        old = response.json()
        new = create(host)
        assert app.state.m2_jobs.repo.candidates() == [old["job_id"], new["job_id"]]
        assert len(combined.get("/api/v1/m2/jobs").json()["items"]) == 1
        assert len(combined.get(P + "/jobs").json()["items"]) == 1
        assert combined.get(P + "/service").json()["counts"]["queued"] == 2
        assert work_one(app.state.m2_jobs) == old["job_id"]
        assert work_one(app.state.m2_jobs) == new["job_id"]
        assert work_one(app.state.m2_jobs) is None
        assert combined.get("/api/v1/m2/jobs/" + old["job_id"] + "/result").status_code == 200
        assert combined.get(P + "/jobs/" + new["job_id"] + "/result").status_code == 200
        assert old["account_id"] != new["account_id"]


def test_dead_publisher_recovers_complete_checkpoint_without_new_trades(host, tmp_path):
    app, jobs, client, _ = host
    job = create(host)
    jid = job["job_id"]
    marker = tmp_path / "publisher-reached.json"
    code = """
import os, signal, sys
from pathlib import Path
from ashare_quant.api import create_app
from ashare_quant.m2_jobs.worker import work_one
database, job_id, marker = sys.argv[1:]
app = create_app(database)
def stage(name, *_):
    if name == "before_publish":
        Path(marker).write_text("native committed; publication not started")
        os.kill(os.getpid(), signal.SIGKILL)
work_one(app.state.m2_jobs, job_id, _stage=stage)
"""
    worker = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(jobs.repo.store.path), jid, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output, _ = worker.communicate(timeout=30)
        assert worker.returncode == -signal.SIGKILL and marker.exists(), output.decode()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)
    checkpoint = jobs.directory(jid) / "native.chk"
    checkpoint_sha = sha(checkpoint.read_bytes())
    assert jobs.get(jid).status == "running" and jobs.get(jid).result is None
    deadline = time.monotonic() + 20
    while not jobs.get(jid).recovery_required:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    result = finish(host, job)
    assert jobs.get(jid).attempt_count == 2
    assert sha(checkpoint.read_bytes()) == checkpoint_sha
    assert len({t["id"] for t in result["trades"]}) == len(result["trades"])
