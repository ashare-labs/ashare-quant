"""Explicit file identity survives source edits and runtime changes without guessing history."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from ashare_quant.strategies.files import StrategyFiles
from ashare_quant.tasks.history import History
from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import execute
from tests.support.daily_release import fixed_release as fixed_release


def test_strategy_history_is_explicit_cross_runtime_and_idempotent(daily_jobs, tmp_path):
    app, jobs, body = daily_jobs
    jobs.product.configured = lambda: SimpleNamespace(
        control_db=tmp_path / "control.sqlite3", document={"profiles": {"product": {}}}, profile="product")
    files = StrategyFiles(tmp_path)
    a, b = "甲/same.py", "乙/same.py"
    for path in (a, b):
        files.mkdir(path.split('/')[0])
        assert files.save(path, body["source"])["strategy_id"] == path
    body.update(end_date=body["start_date"], strategy_id=a)
    with TestClient(app) as client:
        first = execute(client, jobs, body)
        assert first["status"] == "succeeded", first.get("error")
        accepted = jobs.directory(first["job_id"]) / "accepted-manifest.json"
        original = accepted.read_bytes()
        files.save(a, "def initialize(c): pass\n", overwrite=True)
        assert client.post('/api/v1/python/jobs', json=body).json()["job_id"] == first["job_id"]
        conflict = client.post('/api/v1/python/jobs', json={**body, "strategy_id": b})
        assert conflict.status_code == 409
        failed = execute(client, jobs, {**body, "idempotency_key":"revised",
                                       "source":"def initialize(c): raise ValueError('intentional')"})
        assert failed["status"] == "failed"
        pending = client.post('/api/v1/python/jobs', json={**body, "idempotency_key":"cancel"}).json()
        assert client.post('/api/v1/python/jobs/'+pending['job_id']+'/cancel', json={}).status_code == 200
        other = client.post('/api/v1/python/jobs', json={**body, "strategy_id":b, "idempotency_key":"other"}).json()
        legacy = {**body, "idempotency_key":"unassigned"}
        legacy.pop('strategy_id')
        unknown = client.post('/api/v1/python/jobs', json=legacy).json()
        assert 'strategy_id' not in unknown['request']
        result = client.get('/api/v1/python/backtests', params={"strategy_id":a}).json()
        assert result['total'] == 3 and result['unassigned_count'] == 1
        assert {r['stored_status'] for r in result['items']} == {'succeeded','failed','cancelled'}
        success = next(r for r in result['items'] if r['job_id'] == first['job_id'])
        frozen_summary = client.get(first['result']['href']).json()['summary']
        assert all(success['summary'][key] == value for key, value in frozen_summary.items())
        assert success['summary']['benchmark_return'] is None
        assert success['analysis_error'] is None
        assert success['result_available']
        assert success['progress'] == client.get('/api/v1/history/jobs/' + first['job_id']).json()['progress']
        assert success['progress']['elapsed_seconds'] > 0
        assert success['progress']['finished_at'] == success['progress']['sampled_at']
        assert next(r for r in result['items'] if r['job_id'] == pending['job_id'])['progress'] is None
        assert all(r['summary'] is None for r in result['items'] if r['job_id'] != first['job_id'])
        assert client.get('/api/v1/python/backtests',params={"strategy_id":b}).json()['items'][0]['job_id'] == other['job_id']
        assert client.get('/api/v1/python/backtests').json()['items'][0]['job_id'] == unknown['job_id']
        pages = [client.get('/api/v1/python/backtests',params={"strategy_id":a,"limit":1,"offset":n}).json()['items'][0]['job_id'] for n in range(3)]
        assert pages == [r['job_id'] for r in result['items']]
        # A freshly opened reader does not depend on a browser or current strategy bytes.
        restored = History(app.state.store, jobs.config)
        restored.source = 'f' * 64
        app.state.history = restored
        old = client.get('/api/v1/python/backtests',params={"strategy_id":a}).json()
        assert old['total'] == 3
        assert all(r['execution_state'] == 'runtime_unavailable' and not r['can_cancel'] for r in old['items'])
        assert accepted.read_bytes() == original
        assert next(r for r in old['items'] if r['job_id'] == first['job_id'])['progress'] == success['progress']
        assert client.get(success['result']['href']).json()['summary'] == frozen_summary


def test_strategy_binding_refuses_unknown_paths_without_creating_task(daily_jobs, tmp_path):
    app, jobs, body = daily_jobs
    jobs.product.configured = lambda: SimpleNamespace(
        control_db=tmp_path / "control.sqlite3", document={"profiles": {"product": {}}}, profile="product")
    with TestClient(app) as client:
        missing = client.post('/api/v1/python/jobs',json={**body,'strategy_id':'absent.py'})
        assert missing.status_code == 404
        assert jobs.repo.rows() == []
        for identity in ('../escape.py', '/absolute.py', 'not-python.txt'):
            invalid = client.post('/api/v1/python/jobs',json={**body,'strategy_id':identity})
            assert invalid.status_code == 422
        assert client.get('/api/v1/python/backtests?limit=0').status_code == 422
        assert client.get('/api/v1/python/backtests?limit=1&limit=2').status_code == 422
