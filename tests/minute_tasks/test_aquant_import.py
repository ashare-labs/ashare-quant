import pytest
from fastapi.testclient import TestClient

from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_runtime_exports_star_named_and_module_imports_without_legacy_entry(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, end_date=body["start_date"], securities=[], source='''
from aquant import *
from aquant import record as named_record
import aquant as api
import sys
def initialize(context):
    assert api.__name__ == 'aquant'
    assert 'jqdata' not in sys.modules
    assert g is api.g and record is named_record
    assert 'order' in api.__all__ and '__builtins__' not in api.__all__
    run_daily(observe, 'open')
def observe(context):
    named_record(answer=42)
    try: __import__('jq' + 'data')
    except ModuleNotFoundError as error: assert 'AQUANT_LEGACY_IMPORT' in str(error)
    else: raise AssertionError('legacy import accepted')
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["native"]["real_strategy_executed"]
        assert result["native"]["final_account"]["available_cash"] == 100000


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("statement", ["from jqdata import *", "import jqdata as old"])
def test_new_submission_rejects_old_import_without_queueing(daily_jobs, frequency, statement):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, source=statement + "\ndef initialize(c): pass\n")
    with TestClient(app) as client:
        response = client.post("/api/v1/python/jobs", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AQUANT_LEGACY_IMPORT"
        assert "migrate-imports" in response.json()["error"]["message"]
        assert client.get("/api/v1/python/jobs").json()["items"] == []
