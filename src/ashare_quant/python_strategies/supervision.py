"""Local process lifetime and output bounds, not memory or hostile-code isolation."""

import json
import os
import threading
import time

from ashare_quant.contracts import DomainError

from .spec import OUTPUT_LIMIT, PHASE_SECONDS


class BoundedOutput:
    def __init__(self, stream, destination, limit=OUTPUT_LIMIT):
        self.stream, self.destination, self.limit = stream, destination, limit
        self.exceeded = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        size = 0
        try:
            while chunk := os.read(self.stream.fileno(), 65536):
                retained = chunk[: max(0, self.limit - size)]
                self.destination.write(retained)
                size += len(chunk)
                if size > self.limit:
                    self.exceeded.set()
            self.destination.flush()
        except OSError:
            self.failed.set()

    def finish(self):
        self.thread.join(timeout=5)
        if self.thread.is_alive() or self.failed.is_set():
            raise DomainError(
                "M2_JOB_OUTPUT_FAILED", "Child output could not be completely retained"
            )


def check_phase(path, *, module_load_seconds=PHASE_SECONDS, callback_seconds=PHASE_SECONDS):
    if not path.exists():
        return
    phase = json.loads(path.read_text())
    limit = {"MODULE_LOAD": module_load_seconds, "CALLBACK": callback_seconds}.get(phase["phase"])
    if (
        limit is not None
        and time.monotonic() - phase["started_monotonic"] > limit
    ):
        raise DomainError(
            "PYTHON_PHASE_TIMEOUT",
            f"Trusted Python stage exceeded its {limit} second budget",
            details={
                **{key: phase[key] for key in ("phase", "strategy_version_id", "logical_at", "input_sha256")},
                "limit_seconds": limit,
            },
        )
