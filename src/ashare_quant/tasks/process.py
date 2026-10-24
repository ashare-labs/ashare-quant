"""Bound owned child lifetimes and resources; never signal a persisted PID."""

import json
import os
import signal
import subprocess
import threading
import time


def child_limits(policy, parent_pid, action, error_path=None):
    """Run in the isolated child before importing native modules (never preexec_fn)."""
    import resource
    import sys

    for kind, limit in (
        (resource.RLIMIT_CPU, policy["cpu_seconds"]),
        (resource.RLIMIT_FSIZE, policy["file_bytes"]),
        (resource.RLIMIT_NOFILE, policy["open_files"]),
    ):
        _, hard = resource.getrlimit(kind)
        limit = min(limit, hard) if hard != resource.RLIM_INFINITY else limit
        ceiling = limit + 1 if kind == resource.RLIMIT_CPU else limit
        if hard != resource.RLIM_INFINITY:
            ceiling = min(ceiling, hard)
        resource.setrlimit(kind, (limit, ceiling))
    seconds = policy["execution_seconds" if action == "run" else "preparation_seconds"]
    started = time.monotonic()

    def terminate(reason, observed=None):
        limit = seconds if reason == "wall_seconds" else policy.get(reason)
        if reason == "cpu_seconds" and observed is None:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            observed = usage.ru_utime + usage.ru_stime
        if error_path:
            try:
                with open(error_path, "x") as output:
                    json.dump(
                        {
                            "code": "M2_JOB_RESOURCE_LIMIT"
                            if reason != "parent_lost"
                            else "M2_JOB_PARENT_LOST",
                            "message": f"Task reached configured {reason} limit ({limit}); "
                            "no complete result was published" if reason != "parent_lost"
                            else "Task's supervising process ended",
                            "details": {"resource": reason, "observed": observed, "limit": limit},
                        },
                        output,
                    )
                    output.flush()
                    os.fsync(output.fileno())
            except OSError:
                pass  # Full storage must never prevent termination.
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGXCPU, lambda *_: terminate("cpu_seconds"))
    signal.signal(signal.SIGXFSZ, lambda *_: terminate("file_bytes"))

    def watchdog():
        while True:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss = peak if sys.platform == "darwin" else peak * 1024
            if os.getppid() != parent_pid:
                terminate("parent_lost")
            if time.monotonic() - started > seconds:
                terminate("wall_seconds", time.monotonic() - started)
            if rss > policy["rss_bytes"]:
                terminate("rss_bytes", rss)
            time.sleep(0.1)

    threading.Thread(target=watchdog, daemon=True).start()


def stop(process):
    if process is None or getattr(process, "_aq_group_stopped", False):
        return
    process._aq_group_stopped = True
    # All callers create a fresh session. The live Popen handle, not a stored PID,
    # authorizes signalling this group. Kill descendants before reaping the leader.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
