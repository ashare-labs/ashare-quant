"""Small replaceable observations, never task authority or Python checkpoints."""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ashare_quant.storage.artifacts import atomic


def now():
    return datetime.now(timezone.utc).isoformat()


class EngineProgress:
    def __init__(self, root, invocation):
        self.path = Path(root) / "phase.json"
        self.value = {"invocation": invocation, "phase": "preparing",
                      "completed_trading_days": 0, "total_trading_days": None,
                      "simulation_date": None, "peak_rss_bytes": None}
        self.written = 0
        self.update(force=True)

    def update(self, *, force=False, **changes):
        changed_phase = changes.get("phase", self.value["phase"]) != self.value["phase"]
        self.value.update(changes)
        if not (force or changed_phase or time.monotonic() - self.written >= 1):
            return
        try:
            import resource
            import sys
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            self.value["peak_rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
            atomic(self.path, self.value)
            self.written = time.monotonic()
        except (OSError, ValueError):
            pass  # Observation failure must not change execution semantics.


class RuntimeProgress:
    def __init__(self, root, job_id, attempt):
        self.root, self.job_id, self.attempt = Path(root), job_id, attempt
        self.invocation = "attempt-" + str(attempt)
        self.started, self.started_at, self.sequence = time.monotonic(), now(), 0
        self.process, self.sampled_process = None, None
        self.override, self.peak, self.last_current = None, None, None
        self.done, self.lock = threading.Event(), threading.RLock()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.sample()
        self.thread.start()

    def attach(self, process):
        with self.lock:
            self.process = process

    def phase(self, phase):
        with self.lock:
            self.override = phase
        self.sample()

    def _watch(self):
        while not self.done.wait(2):
            self.sample()

    def sample(self, *, terminal=False):
        try:
            with self.lock:
                child = {}
                path = self.root / "phase.json"
                if path.is_file() and not path.is_symlink():
                    child = json.loads(path.read_text())
                    if child.get("invocation") != self.invocation:
                        child = {}
                current = None
                if self.process is not None and self.process.poll() is None:
                    import psutil
                    try:
                        if self.sampled_process is None:
                            self.sampled_process = psutil.Process(self.process.pid)
                        # psutil checks identity on is_running; never use a saved PID
                        # to signal or accidentally display a later unrelated process.
                        if self.sampled_process.is_running():
                            current = self.sampled_process.memory_info().rss
                    except psutil.Error:
                        pass
                child_peak = child.get("peak_rss_bytes")
                samples = [n for n in (self.peak, current, child_peak)
                           if type(n) is int and n >= 0]
                self.peak = max(samples) if samples else None
                if current is not None:
                    self.last_current = current
                self.sequence += 1
                sampled_at = now()
                value = {"job_id": self.job_id, "attempt": self.attempt,
                         "sequence": self.sequence, "sampled_at": sampled_at,
                         "started_at": self.started_at,
                         "finished_at": sampled_at if terminal else None,
                         "elapsed_seconds": max(0, time.monotonic() - self.started),
                         "phase": self.override or child.get("phase", "preparing"),
                         "completed_trading_days": child.get("completed_trading_days"),
                         "total_trading_days": child.get("total_trading_days"),
                         "simulation_date": child.get("simulation_date"),
                         "current_rss_bytes": current if not terminal else None,
                         "last_rss_bytes": self.last_current, "peak_rss_bytes": self.peak,
                         "memory_scope": "strategy_process", "terminal": terminal}
                atomic(self.root / "progress.json", value)
        except (OSError, ValueError, TypeError, ImportError):
            pass

    def finish(self):
        self.done.set()
        self.thread.join()
        self.sample(terminal=True)


def snapshot(root, row):
    """Read one small current-attempt snapshot; absent telemetry is not an error."""
    try:
        from ashare_quant.tasks.minute.contracts import TaskProgress

        path = Path(root) / "progress.json"
        if not path.is_file() or path.is_symlink():
            return None
        value = TaskProgress.model_validate_json(path.read_bytes())
        if value.job_id != row["job_id"] or value.attempt != row["attempt_count"]:
            return None
        # Older final snapshots recorded the finish observation as sampled_at.
        # A database terminal state alone cannot turn a stale sample into a
        # measured full duration (e.g. the worker died before its final sample).
        if value.terminal and value.finished_at is None:
            value = value.model_copy(update={"finished_at": value.sampled_at})
        terminal = row["status"] in {"succeeded", "failed", "cancelled"}
        if terminal:
            value = value.model_copy(update={"terminal": True, "current_rss_bytes": None})
        return value.model_dump(mode="json")
    except (OSError, ValueError, TypeError):
        return None
