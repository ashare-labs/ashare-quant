"""Bounded native process slots, fenced leases and retained atomic checkpoints."""

import json
import subprocess
import time

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import lock, regular, worker_slot
from ashare_quant.strategies.python.supervision import BoundedOutput, check_phase
from ashare_quant.tasks.process import stop
from ashare_quant.tasks.storage_budget import check_task_storage


def work_one(jobs, job_id=None, *, _fault="none", _stage=None):
    """Internal fault/stage arguments are test-only and have no HTTP/CLI input."""
    jobs.guard()
    ids = [job_id] if job_id else jobs.repo.candidates()
    if not ids:
        return None
    try:
        with worker_slot(jobs.root, jobs.repo.policy.max_workers) as serial:
            for candidate in ids:
                from ashare_quant.tasks.contracts import ACTIVE_SCHEMAS

                # Do not even create a lock file in retained historical tasks.
                row = jobs.repo.row(candidate)
                frozen = json.loads(row["manifest"])
                if jobs.active_queue and frozen.get("schema_version") not in ACTIVE_SCHEMAS:
                    if job_id:
                        jobs.for_job(row)
                    continue
                if jobs.active_queue and frozen.get("service_identity", {}).get("source_sha256") != jobs.source:
                    if job_id:
                        raise DomainError("M2_JOB_SERVICE_CHANGED", "Execution requires the task's original runtime", 409)
                    continue
                try:
                    with lock(jobs.directory(candidate) / "host-writer.lock") as writer:
                        row = jobs.repo.row(candidate)
                        try:
                            target = jobs.for_job(row)
                            manifest = target.check(row, execution=True)
                        except DomainError as exc:
                            refused = jobs.repo.claim(candidate)
                            if refused:
                                jobs.repo.finish(
                                    candidate,
                                    refused["lease_token"],
                                    error={"code": exc.code, "message": str(exc)},
                                )
                                return candidate
                            raise
                        claimed = jobs.repo.claim(candidate)
                        if claimed is None:
                            continue
                        _work(
                            target,
                            claimed,
                            manifest,
                            (serial.fileno(), writer.fileno()),
                            _fault,
                            _stage,
                        )
                        return candidate
                except DomainError as exc:
                    if exc.code == "M2_JOB_WRITER_BUSY" and job_id is None:
                        continue
                    raise
    except DomainError as exc:
        if exc.code == "M2_JOB_WRITER_BUSY" and job_id is None:
            return None
        raise
    return None


def _work(jobs, row, manifest, lock_fds, fault, stage):
    job_id, token = row["job_id"], row["lease_token"]
    root = jobs.directory(job_id)
    invocation = "attempt-" + str(row["attempt_count"])
    process, proof, reference, error = None, None, None, None
    interrupted = False
    capture = None

    def notify(name):
        if stage:
            stage(name, jobs, row, process)

    try:
        notify("claimed")
        cancelled = jobs.repo.heartbeat(job_id, token)
        if not cancelled:
            check_task_storage(root, jobs.repo.policy)
            command = jobs.executor.command(
                "run",
                root,
                *jobs.executor_args(manifest),
                invocation,
                manifest.session_file_sha256,
                fault=fault,
                control={"database": str(jobs.repo.store.path), "job_id": job_id, "token": token},
            )
            jobs.repo.starting(job_id, token)
            with (root / (invocation + ".process.log")).open("wb") as log:
                process = subprocess.Popen(
                    command,
                    env=jobs.executor.environment(),
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    pass_fds=lock_fds,
                    start_new_session=True,
                )
                capture = BoundedOutput(process.stdout, log, jobs.repo.policy.output_bytes)
                try:
                    jobs.repo.child_started(job_id, token, process.pid)
                    notify("spawned")
                    started, heartbeat = time.monotonic(), 0
                    while process.poll() is None:
                        notify("poll")
                        if capture.exceeded.is_set():
                            raise DomainError(
                                "PYTHON_OUTPUT_LIMIT", "Attempt output exceeded its fixed byte limit"
                            )
                        check_phase(
                            root / (invocation + ".python-phase.json"),
                            module_load_seconds=jobs.repo.policy.module_load_seconds,
                            callback_seconds=jobs.repo.policy.callback_seconds,
                        )
                        if time.monotonic() - heartbeat >= 0.25:
                            check_task_storage(root, jobs.repo.policy)
                            if jobs.repo.heartbeat(job_id, token):
                                stop(process)
                                cancelled = True
                                break
                            heartbeat = time.monotonic()
                        if time.monotonic() - started > jobs.repo.policy.execution_seconds:
                            stop(process)
                            raise DomainError(
                                "M2_JOB_EXECUTION_TIMEOUT",
                                "Bounded native child exceeded its execution timeout",
                            )
                        time.sleep(0.03)
                finally:
                    stop(process)
                    try:
                        capture.finish()
                    finally:
                        process.stdout.close()
                if capture.exceeded.is_set():
                    raise DomainError("PYTHON_OUTPUT_LIMIT", "Attempt output exceeded its fixed byte limit")
            notify("child_exited")
            check_task_storage(root, jobs.repo.policy)
            if process.returncode == 0:
                proof = json.loads(regular(root / (invocation + ".proof.json")))
            elif not cancelled:
                error = {
                    "code": "M2_JOB_NATIVE_STOPPED",
                    "returncode": process.returncode,
                    "message": "Native attempt stopped; only the last committed checkpoint may resume",
                }
        if error and process is not None:
            for error_name in (invocation + ".resource-limit.json", invocation + ".runner-error.json", invocation + ".failure.json"):
                error_path = root / error_name
                if error_path.exists():
                    reported = json.loads(regular(error_path))
                    if str(reported.get("code", "")).startswith(("PYTHON_", "PORTFOLIO_", "FACTOR_")) or reported.get("code") in {
                        "M2_JOB_RESOURCE_LIMIT", "M2_JOB_PARENT_LOST",
                    }:
                        error = {**reported, "returncode": process.returncode}
                        break
        jobs.repo.heartbeat(job_id, token)
        if proof is None:
            proof = jobs.executor.immediate(
                "inspect",
                root,
                *jobs.executor_args(manifest),
                invocation + "-inspect",
                manifest.session_file_sha256,
                lock_fds=lock_fds,
            )
        notify("inspected")
        jobs.guard()
        cancelled = jobs.repo.heartbeat(job_id, token)
        if not cancelled and error is None and proof["complete"]:
            notify("before_publish")
            reference = jobs.publish(
                root, manifest, invocation, row["attempt_count"], jobs.repo.logs(job_id)
            )
            notify("after_publish")
            check_task_storage(root, jobs.repo.policy)
            jobs.guard()
        elif not cancelled and error is None:
            error = {
                "code": "M2_JOB_INCOMPLETE",
                "message": "Native execution did not commit a complete receipt",
            }
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        stop(process)
        error = {
            "code": getattr(exc, "code", "M2_JOB_WORKER_FAILED"),
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "details": getattr(exc, "details", None),
        }
        reference = None
    finally:
        stop(process)
    if proof is None and process is not None:
        try:
            proof = jobs.executor.immediate(
                "inspect",
                root,
                *jobs.executor_args(manifest),
                invocation + "-error-inspect",
                manifest.session_file_sha256,
                lock_fds=lock_fds,
            )
        except Exception as exc:
            if error is not None:
                error["inspection_error"] = getattr(exc, "code", type(exc).__name__)
    # Conditional UPDATE under an immediate transaction resolves cancellation
    # versus publication. A stale worker can never turn the row into success.
    jobs.repo.finish(job_id, token, proof=proof, result=reference, error=error)
    if interrupted:
        raise KeyboardInterrupt
