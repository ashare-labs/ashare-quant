"""Kill actual native processes; compare their resumed books to uninterrupted runs."""

import json
import signal
import subprocess
import sys
import time

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.service import PaperService

from .fixtures import config, queue


def start(root):
    service = PaperService(root)
    session = config(root.name)
    sid = service.create(session)["id"]
    day = queue(service, sid, session, 0, (100, 0))
    first = service.work(sid, day)
    day = queue(service, sid, session, 1, (0, 100), first["checkpoint_sha256"])
    return service, sid, day, first


def economics(report):
    native = report["native"]
    return {
        k: native[k]
        for k in (
            "before",
            "account",
            "orders",
            "trades",
            "events",
            "reconciliation",
            "cursor",
            "settlement_count",
            "target_gaps",
        )
    }


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory):
    service, sid, day, _ = start(tmp_path_factory.mktemp("reference"))
    return economics(service.work(sid, day))


@pytest.mark.parametrize(
    "fault",
    [
        "inside_native_trade",
        "inside_native_submit",
        "inside_native_cancel",
        "inside_native_settlement",
        "event_committed:1",
        "event_committed:2",
        "event_committed:3",
        "event_committed:4",
        "after_complete_checkpoint",
        "checkpoint:before_replace:6",
        "checkpoint:after_replace:6",
    ],
)
def test_actual_native_kill_and_resume(tmp_path, uninterrupted, fault):
    service, sid, day, first = start(tmp_path)
    with pytest.raises(DomainError, match="Native worker exited -9"):
        service.work(sid, day, fault=fault)
    status = service.status(sid)
    assert status["session"]["head"] == first["checkpoint_sha256"]
    assert status["days"][-1]["recovery_required"]
    assert any(e["kind"] == "alert" for e in status["events"])
    with pytest.raises(DomainError) as blocked:
        service.work(sid, day)
    assert blocked.value.code == "PAPER_RESUME_REQUIRED"
    service.control(sid, "resume", first["checkpoint_sha256"], "controlled recovery")
    report = service.work(sid, day)
    assert economics(report) == uninterrupted
    assert service.work(sid, day) == report
    events = service.status(sid)["events"]
    if fault == "after_complete_checkpoint":
        assert events[-1]["details"]["native_invoked"] is False


@pytest.mark.parametrize("point", ["spawned", "before_publish"])
def test_controller_sigkill_and_orphan_recovery(tmp_path, uninterrupted, point):
    service, sid, day, first = start(tmp_path)
    marker = tmp_path / "child.json"
    boot = """
import json,os,signal,sys
from pathlib import Path
from ashare_quant.paper.service import PaperService
root,sid,day,point,marker=json.loads(sys.argv[1])
def stage(where, child):
    if where == point:
        Path(marker).write_text(json.dumps({'child':child.pid, 'stage':where}))
        os.kill(os.getpid(), signal.SIGKILL)
PaperService(root).work(sid,day,stage=stage)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            boot,
            json.dumps([str(tmp_path), sid, day, point, str(marker)]),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output, _ = process.communicate(timeout=25)
    assert process.returncode == -signal.SIGKILL, output.decode()
    assert json.loads(marker.read_text())["stage"] == point
    # The child holds the inherited writer fd until its parent-death guard exits.
    end = time.monotonic() + 8
    while True:
        try:
            service.control(sid, "resume", first["checkpoint_sha256"], "controller recovery")
            break
        except DomainError as exc:
            assert exc.code == "M2_JOB_WRITER_BUSY"
            assert time.monotonic() < end
            time.sleep(0.1)
    assert economics(service.work(sid, day)) == uninterrupted


def test_running_writer_fences_duplicate_and_stale_lease(tmp_path):
    service, sid, day, first = start(tmp_path)
    checks = []

    def stage(where, child):
        if where != "spawned":
            return
        with service.db() as db:
            db.execute(
                "UPDATE paper_days SET lease_until=0 WHERE session_id=? AND day=?", (sid, day)
            )
        with pytest.raises(DomainError) as busy:
            PaperService(tmp_path).work(sid, day)
        checks.append(busy.value.code)

    with pytest.raises(DomainError) as lost:
        service.work(sid, day, stage=stage)
    assert lost.value.code == "PAPER_FENCE_LOST"
    assert checks == ["M2_JOB_WRITER_BUSY"]
    assert service.status(sid)["session"]["head"] == first["checkpoint_sha256"]
    service.control(sid, "resume", first["checkpoint_sha256"], "fenced retry")
    assert service.work(sid, day)["native"]["reconciliation"]["status"] == "passed"
