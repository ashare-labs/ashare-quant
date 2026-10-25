"""Bound owned child lifetimes and resources; never signal a persisted PID."""

import json
import os
import signal
import subprocess
import threading
import time


def child_limits(policy, parent_pid, action, error_path=None):
    """Run in the isolated child before importing native modules (never preexec_fn)."""
    # No fixed RSS, CPU, wall-time, file-size or descriptor ceilings. Keep the
    # orphan guard independent of user code; only the owned process group is stopped.
    def watch():
        while True:
            if os.getppid() != parent_pid:
                if error_path:
                    try:
                        with open(error_path, "x") as output:
                            json.dump({"code": "M2_JOB_PARENT_LOST",
                                       "message": "Owning worker exited; no complete result was published"}, output)
                            output.flush()
                            os.fsync(output.fileno())
                    except OSError:
                        pass
                os.killpg(os.getpgrp(), signal.SIGKILL)
            time.sleep(0.1)

    threading.Thread(target=watch, daemon=True).start()


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
