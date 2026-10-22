"""Known numerical answers plus immutable HTTP/task lifecycle boundaries."""

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.factors.catalog import catalogue
from ashare_quant.factors.compute import compute
from ashare_quant.factors.inputs import Sources
from ashare_quant.storage.artifacts import atomic, sha
from ashare_quant.tasks.daily.service import Jobs
from ashare_quant.tasks.factors.contracts import FactorCreate
from ashare_quant.tasks.worker import work_one


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


def test_catalogue_and_conservative_version(app):
    with TestClient(app) as client:
        c = client.get("/api/v1/factors/catalogue").json()
        assert len(c["items"]) == 210
        assert len([r for r in c["items"] if r["library"] == "joinquant"]) == 19
        item = next(r for r in c["items"] if r["field"] == "alpha014")
        assert item["expression"] == "DELTA(C, 5)" and item["warmup_rows"] == 6
        conservative = client.get("/api/v1/factors/catalogue?version=gtja191.public.v1")
        assert conservative.status_code == 200
        assert (
            sum(r["availability"] == "AMBIGUOUS_FORMULA" for r in conservative.json()["items"]) == 8
        )
        assert client.get("/api/v1/factors/catalogue?version=unknown").status_code == 422


def test_real_process_duplicate_frozen_source_and_refresh(app, source):
    with TestClient(app) as client:
        body = request(app)
        created = client.post("/api/v1/factors/jobs", json=body)
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]
        assert client.post("/api/v1/factors/jobs", json=body).json()["job_id"] == job_id
        assert (
            client.post("/api/v1/factors/jobs", json={**body, "name": "different"}).status_code
            == 409
        )
        # Submission freezes the selected data, so later source changes cannot
        # change the queued computation or its saved result.
        Path(source[0]["fixture"]["path"]).write_text("{}")
        assert work_one(Jobs(app.state.factor_jobs.repo.store), job_id) == job_id
        job = client.get("/api/v1/factors/jobs/" + job_id).json()
        assert job["status"] == "succeeded", job
        result = client.get("/api/v1/factors/jobs/" + job_id + "/result").json()
        assert result["summary"]["valid_cells"] == 14
        assert result["summary"]["missing_cells"] == 10
        assert {r["value"] for r in result["rows"] if r["value"] is not None} == {-5.0, 5.0}
        assert all(r["status"] == "WARMUP" for r in result["rows"][:10])
        assert client.get("/api/v1/tasks/daily/jobs").json()["items"] == []
        restored = create_app(str(app.state.factor_jobs.repo.store.path), factor_sources=source[0])
        with TestClient(restored) as again:
            assert again.get("/api/v1/factors/jobs/" + job_id + "/result").json() == result
            assert again.get("/api/v1/factors/jobs").json()["items"][0]["attempt_count"] == 1


def test_missing_dependency_not_a_fake_zero(app, source):
    body = FactorCreate.model_validate({**request(app), "factor_id": "alpha191:alpha150"})
    frozen = Sources(source[0]).freeze(body)
    result = compute(frozen, body, "m2job-" + "1" * 32)
    assert result["summary"]["valid_cells"] == 0
    assert all(r["value"] is None and r["status"] == "MISSING_DEPENDENCY" for r in result["rows"])


def test_parameters_scope_and_tampering(app):
    with TestClient(app) as client:
        for patch in (
            {"history_start": "2024-02-01"},
            {"securities": ["000001.XSHE"] * 2},
            {"factor_id": "alpha191:alpha146"},
            {"parameters": {"bogus": 5}},
        ):
            response = client.post("/api/v1/factors/jobs", json={**request(app), **patch})
            assert response.status_code in (409, 422), response.text
        accepted = client.post("/api/v1/factors/jobs", json=request(app)).json()
        root = app.state.factor_jobs.directory(accepted["job_id"])
        atomic(root / "input.json", {})
        response = client.get("/api/v1/factors/jobs/" + accepted["job_id"])
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "FACTOR_INPUT_CHANGED"


def test_queued_and_running_cancel_never_publish(app):
    jobs = app.state.factor_jobs
    first = jobs.create(FactorCreate.model_validate(request(app)))
    assert jobs.cancel(first.job_id).status == "cancelled"
    with pytest.raises(DomainError, match="No completed result"):
        jobs.result(first.job_id)
    second = jobs.create(
        FactorCreate.model_validate({**request(app), "idempotency_key": "cancel-running"})
    )

    def stage(name, service, row, process):
        if name == "after_publish":
            service.cancel(row["job_id"])

    work_one(jobs, second.job_id, _stage=stage)
    assert jobs.get(second.job_id).status == "cancelled", jobs.get(second.job_id)
    with pytest.raises(DomainError):
        jobs.result(second.job_id)


def test_joinquant_existing_amount_formula(tmp_path):
    from ashare_data.factors.joinquant.panel import DEFINITION, UNITS

    days = pd.bdate_range("2024-01-02", periods=7).strftime("%Y-%m-%d").tolist()
    bars = {
        d: dict(open="10", high="11", low="9", close="10", volume="100", amount=str(1000 + i * 100))
        for i, d in enumerate(days)
    }
    payload = dict(
        schema="ad.factor-panel-batch-inputs.v1",
        sessions=days,
        raw_price_adjustment="none",
        input_contains_JQ_values=False,
        units=UNITS,
        items=[dict(security="000001.XSHE", sina=bars, factors=[], shares=[], financials=[])],
    )
    path = tmp_path / "jq.json"
    atomic(path, payload)
    sources = {
        "fixture": dict(kind="joinquant_panel", path=str(path), sha256=sha(path.read_bytes()))
    }
    body = FactorCreate(
        environment_id="a" * 64,
        idempotency_key="jq",
        name="amount",
        factor_id="joinquant:TVMA6",
        definition=DEFINITION,
        source_id="fixture",
        securities=["000001.XSHE"],
        history_start=days[0],
        start_date=days[0],
        end_date=days[-1],
    )
    result = compute(Sources(sources).freeze(body), body, "m2job-" + "2" * 32)
    assert [r["value"] for r in result["rows"]][-2:] == [1250.0, 1350.0]
    assert all(r["value"] is None for r in result["rows"][:5])
    assert "TVMA6" in [r["field"] for r in catalogue()]


@pytest.mark.parametrize("change", ["nonboolean", "shortrow", "nonfinite", "source_changed"])
def test_malformed_inputs_are_rejected_before_admission(tmp_path, source, change):
    specs, payload = source
    if change == "nonboolean":
        payload["universe"][0][0] = "false"
    elif change == "shortrow":
        payload["fields"]["close"][0] = [1]
    else:
        payload["fields"]["close"][0][0] = "Infinity"
    path = Path(specs["fixture"]["path"])
    atomic(path, payload)
    if change != "source_changed":
        specs["fixture"]["sha256"] = sha(path.read_bytes())
    app = create_app(str(tmp_path / "malformed.sqlite3"), factor_sources=specs)
    with TestClient(app) as client:
        response = client.post("/api/v1/factors/jobs", json=request(app))
        assert response.status_code in (409, 422), response.text
        assert client.get("/api/v1/factors/jobs").json()["items"] == []


def test_gap_and_membership_are_preserved(source):
    specs, payload = source
    payload["fields"]["close"][6][0] = None
    payload["universe"][8][1] = False
    path = Path(specs["fixture"]["path"])
    atomic(path, payload)
    specs["fixture"]["sha256"] = sha(path.read_bytes())
    body = FactorCreate(
        environment_id="a" * 64,
        idempotency_key="gaps",
        name="gaps",
        factor_id="alpha191:alpha014",
        definition="gtja191.explicit.v1",
        source_id="fixture",
        securities=payload["securities"],
        history_start=payload["sessions"][0],
        start_date=payload["sessions"][0],
        end_date=payload["sessions"][-1],
    )
    result = compute(Sources(specs).freeze(body), body, "m2job-" + "4" * 32)
    assert len(result["rows"]) == 24
    rows = {(r["date"], r["security"]): r for r in result["rows"]}
    assert rows[(payload["sessions"][6], payload["securities"][0])]["value"] is None
    assert rows[(payload["sessions"][8], payload["securities"][1])]["status"] == "OUT_OF_UNIVERSE"


def test_evaluation_through_real_worker_and_label_buffer(tmp_path, source):
    from ashare_quant.factors.compute import evaluation_available

    if not evaluation_available():
        pytest.skip("Optional ashare-data[factor-evaluation] is not installed")
    specs, payload = source
    payload["adjustment"] = "adjusted:synthetic-no-split-fixture"
    path = Path(specs["fixture"]["path"])
    atomic(path, payload)
    specs["fixture"]["sha256"] = sha(path.read_bytes())
    app = create_app(str(tmp_path / "evaluation.sqlite3"), factor_sources=specs)
    with TestClient(app) as client:
        body = {
            **request(app),
            "start_date": "2024-01-06",
            "end_date": "2024-01-12",
            "label_end": "2024-01-17",
            "evaluation": {
                "horizons": [1, 3],
                "quantiles": 2,
                "min_assets": 2,
                "rolling_window": 2,
            },
        }
        response = client.post("/api/v1/factors/jobs", json=body)
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        work_one(Jobs(app.state.factor_jobs.repo.store), job_id)
        job = client.get("/api/v1/factors/jobs/" + job_id).json()
        assert job["status"] == "succeeded", job
        result = client.get("/api/v1/factors/jobs/" + job_id + "/result").json()
        evaluation = result["evaluation"]
        assert evaluation["schema"] == "ad.factor-evaluation.v1"
        assert evaluation["config"]["start_date"] == "2024-01-08"
        assert evaluation["config"]["end_date"] == "2024-01-12"
        assert evaluation["config"]["input_id"] == result["provenance"]["input_sha256"]
        assert evaluation["coverage"]["input_cells"] == 10
        assert all(r["date"] <= "2024-01-12" for r in result["rows"])
        three = next(r for r in evaluation["coverage"]["by_horizon"] if r["horizon"] == 3)
        assert three["excluded"]["insufficient_label_buffer"] == 2
        assert any(r["value"] is not None for r in evaluation["ic"])
        assert client.post("/api/v1/factors/jobs", json=body).json()["job_id"] == job_id


def test_raw_prices_cannot_silently_become_evaluation_labels(source):
    specs, payload = source
    body = FactorCreate(
        environment_id="a" * 64,
        idempotency_key="basis",
        name="basis",
        factor_id="alpha191:alpha014",
        definition="gtja191.explicit.v1",
        source_id="fixture",
        securities=payload["securities"],
        history_start=payload["sessions"][0],
        start_date=payload["sessions"][0],
        end_date=payload["sessions"][-1],
        evaluation={},
    )
    with pytest.raises(DomainError, match="declared adjusted close"):
        Sources(specs).freeze(body)


def test_foreign_or_malformed_task_identity_is_an_explicit_conflict(app):
    for raw in ('{"schema_version":"python-minute-job.v1"}', "[]", "invalid"):
        with pytest.raises(DomainError) as error:
            app.state.factor_jobs.repo.manifest({"manifest": raw})
        assert error.value.code == "M2_JOB_IDENTITY_CONFLICT"
        assert error.value.status == 409
