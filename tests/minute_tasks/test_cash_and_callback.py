"""Cash has the explicit 100亿 boundary; callbacks share real attempt resources."""

import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.tasks.minute.contracts import PythonCreate
from tests.minute_tasks.test_daily import daily_jobs as daily_jobs
from tests.minute_tasks.test_daily import execute
from tests.minute_tasks.test_daily import fixed_release as fixed_release


@pytest.mark.parametrize("fixed_release", [{"minutes": True}], indirect=True)
@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_100_yi_capital_actual_trade_preserves_fees_and_t_plus_one(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, initial_cash=10_000_000_000, end_date=body["start_date"], source="""from aquant import *
def initialize(context):
    run_daily(trade, time='open')
def trade(context):
    order('600000.XSHG', 100)
""")
    assert math.ulp(body["initial_cash"]) < .01
    for cash in (10_000_000_000.01, 0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValidationError):
            PythonCreate.model_validate(body | {"initial_cash": cash})
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(f"/api/v1/python/jobs/{job['job_id']}/result").json()
        assert result["summary"]["initial_cash"] == 10_000_000_000
        native = result["native"]
        assert len(native["trades"]) == 1 and native["trades"][0]["amount"] == 100
        assert native["trades"][0]["commission"] == 5
        assert native["final_account"]["positions"]["600000.XSHG"]["closeable_amount"] == 0
        assert native["final_account"]["starting_cash"] == 10_000_000_000


@pytest.mark.parametrize("daily_jobs,delay,status", [({}, 6, "succeeded"),
                                                     ({"execution_seconds": 2}, 5, "failed")], indirect=["daily_jobs"])
def test_no_unadvertised_callback_gate_but_actual_attempt_timeout(daily_jobs, delay, status):
    app, jobs, body = daily_jobs
    body.update(securities=[], end_date=body["start_date"],
                source=f"import time\ndef initialize(context): time.sleep({delay})\n")
    with TestClient(app) as client:
        policy = client.get("/api/v1/tasks/service").json()["policy"]
        assert not {"module_load_seconds", "callback_seconds"} & policy.keys()
        job = execute(client, jobs, body)
        assert job["status"] == status, job
        if status == "failed":
            assert job["error"]["code"] in {"M2_JOB_EXECUTION_TIMEOUT", "M2_JOB_RESOURCE_LIMIT"}
            assert client.get(f"/api/v1/python/jobs/{job['job_id']}/result").status_code == 409
