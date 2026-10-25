"""Terminal refresh avoids repeated input bytes while still detecting every mutation."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.tasks.worker import work_one
from tests.support.factor_app import app as app
from tests.support.factor_app import request
from tests.support.factor_app import source as source


@pytest.mark.parametrize('change', ['same_size_restored_mtime', 'replace', 'symlink', 'missing'])
def test_completed_refresh_verifies_once_but_file_changes_invalidate(app, source, monkeypatch, change):
    import ashare_quant.tasks.factors.service as module

    jobs = app.state.factor_jobs
    with TestClient(app) as client:
        response = client.post('/api/v1/factors/jobs', json=request(app))
        assert response.status_code == 201, response.text
        job_id = response.json()['job_id']
        assert work_one(jobs, job_id) == job_id
    # Reopening the service must not rely on an earlier process's verification.
    restored = create_app(str(jobs.repo.store.path), factor_sources=source[0])
    reads = []
    original = module.regular

    def measured(path):
        raw = original(path)
        if Path(path).name == 'input.json':
            reads.append(len(raw))
        return raw

    monkeypatch.setattr(module, 'regular', measured)
    with TestClient(restored) as client:
        for _ in range(3):
            for suffix in ('', '/' + job_id, '/' + job_id + '/logs'):
                assert client.get('/api/v1/factors/jobs' + suffix).status_code == 200
        assert len(reads) == 1
        path = jobs.directory(job_id) / 'input.json'
        raw, old = path.read_bytes(), path.stat()
        if change == 'same_size_restored_mtime':
            path.write_bytes(b'!' + raw[1:])
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        elif change == 'replace':
            replacement = path.with_name('replacement.json')
            replacement.write_bytes(b'!' + raw[1:])
            os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
            replacement.replace(path)
        elif change == 'symlink':
            original_path = path.with_name('original-input.json')
            path.rename(original_path)
            path.symlink_to(original_path)
        else:
            path.unlink()
        response = client.get('/api/v1/factors/jobs/' + job_id)
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] in {'FACTOR_INPUT_CHANGED', 'M2_JOB_ARTIFACT_UNAVAILABLE'}
