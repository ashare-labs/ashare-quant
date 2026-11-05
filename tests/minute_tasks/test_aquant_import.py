import pytest
from fastapi.testclient import TestClient

from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_runtime_exports_star_named_and_module_imports_with_jqdata_alias(daily_jobs, frequency):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, end_date=body["start_date"], securities=[], source='''
from aquant import *
from aquant import record as named_record
import aquant as api
import jqdata as legacy_api
import sys
def initialize(context):
    assert api.__name__ == 'aquant'
    assert sys.modules['jqdata'] is api and legacy_api is api
    assert g is api.g and record is named_record
    assert 'order' in api.__all__ and '__builtins__' not in api.__all__
    run_daily(observe, 'open')
def observe(context):
    named_record(answer=42)
    assert __import__('jq' + 'data') is api
''')
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(job["result"]["href"]).json()
        assert result["native"]["real_strategy_executed"]
        assert result["native"]["final_account"]["available_cash"] == 100000


@pytest.mark.parametrize("frequency", ["1d", "1m"])
@pytest.mark.parametrize("statement", ["from jqdata import *", "import jqdata as old"])
def test_new_submission_accepts_legacy_jqdata_import_alias(daily_jobs, frequency, statement):
    app, jobs, body = daily_jobs
    body.update(frequency=frequency, end_date=body["start_date"], securities=[], source=statement + "\ndef initialize(c): pass\n")
    with TestClient(app) as client:
        job = execute(client, jobs, body)
        assert job["status"] == "succeeded", job.get("error")
