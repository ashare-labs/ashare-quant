"""Drain child output without pipe deadlocks; task storage remains independently checked."""

import os
import threading

from ashare_quant.contracts import DomainError


class ProcessOutput:
    def __init__(self, stream, destination):
        self.stream, self.destination = stream, destination
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while chunk := os.read(self.stream.fileno(), 65536):
                self.destination.write(chunk)
            self.destination.flush()
        except OSError:
            self.failed.set()

    def finish(self):
        self.thread.join(timeout=5)
        if self.thread.is_alive() or self.failed.is_set():
            raise DomainError(
                "M2_JOB_OUTPUT_FAILED", "Child output could not be completely retained"
            )
