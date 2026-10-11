import json
import os
import signal
import subprocess
import sys
import time

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import lock, sha
from ashare_quant.m2_jobs.worker import work_one

from .conftest import create

CHILD = r"""
import json, os, signal, sys, time
from pathlib import Path
from ashare_quant.store import Store
from ashare_quant.m2_jobs.executor import Configuration
from ashare_quant.m2_jobs.service import Jobs
from ashare_quant.m2_jobs.worker import work_one
db, owner, job_id, marker, mode = sys.argv[1:]
jobs = Jobs(Store(db), Configuration(Path(owner)))
def stage(name, service, row, process):
    if mode == "orphan" and name == "spawned":
        os.kill(process.pid, signal.SIGSTOP)
        Path(marker).write_text(json.dumps({"pid": process.pid, "worker": os.getpid()}))
        while True:
            time.sleep(0.1)
    if mode == "publication" and name == "before_publish":
        Path(marker).write_text(json.dumps({"worker": os.getpid()}))
        os.kill(os.getpid(), signal.SIGKILL)
work_one(jobs, job_id, _stage=stage)
"""


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("controlled subprocess did not reach expected state")


def spawn(jobs, job_id, marker, mode, log):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD,
            str(jobs.repo.store.path),
            str(jobs.config.owner_store),
            job_id,
            str(marker),
            mode,
        ],
        env=os.environ | {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def test_dead_worker_inherited_lock_and_orphan_watchdog(host, tmp_path):
    jobs, client = host
    job_id = create(client)["job_id"]
    marker = tmp_path / "orphan.json"
    child_pid = None
    with (tmp_path / "orphan-worker.log").open("wb") as log:
        worker = spawn(jobs, job_id, marker, "orphan", log)
        try:
            wait_for(marker.exists)
            child_pid = json.loads(marker.read_text())["pid"]
            worker.kill()
            assert worker.wait(timeout=5) == -signal.SIGKILL
            with jobs.repo.store.connection(transaction=True) as db:
                db.execute("UPDATE m2_jobs SET lease_until=0 WHERE job_id=?", (job_id,))
            # Worker is dead, but its stopped native child still owns both fd locks.
            with pytest.raises(DomainError) as exc:
                work_one(jobs, job_id)
            assert exc.value.code == "M2_JOB_WRITER_BUSY"
            os.kill(child_pid, signal.SIGCONT)

            def released():
                try:
                    with lock(jobs.root / "worker.lock"):
                        return True
                except DomainError:
                    return False

            wait_for(released)
            child_pid = None
            assert jobs.get(job_id).recovery_required
            work_one(jobs, job_id)
            assert jobs.get(job_id).status == "succeeded"
            assert jobs.get(job_id).attempt_count == 2
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_real_worker_death_after_native_completion_recovers_without_rerun(host, tmp_path):
    jobs, client = host
    job_id = create(client)["job_id"]
    marker = tmp_path / "publication.json"
    with (tmp_path / "dead-publisher.log").open("wb") as log:
        worker = spawn(jobs, job_id, marker, "publication", log)
        try:
            wait_for(marker.exists)
            assert worker.wait(timeout=5) == -signal.SIGKILL
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
    checkpoint = sha((jobs.directory(job_id) / "native.chk").read_bytes())
    assert jobs.get(job_id).status == "running" and jobs.get(job_id).result is None
    # Exercise the real lease duration instead of editing the lease field.
    wait_for(lambda: jobs.get(job_id).recovery_required, timeout=20)
    work_one(jobs, job_id)
    result = jobs.result(job_id)
    assert result.recovery["completion"]["native_run"] is False
    assert sha((jobs.directory(job_id) / "native.chk").read_bytes()) == checkpoint


def test_keyboard_interrupt_stops_worker_without_losing_task(host):
    jobs, client = host
    job_id = create(client)["job_id"]

    def stage(name, *args):
        if name == "spawned":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        work_one(jobs, job_id, _stage=stage)
    assert jobs.get(job_id).status == "failed"
    jobs.resume(job_id)
    work_one(jobs, job_id)
    assert jobs.get(job_id).status == "succeeded"
