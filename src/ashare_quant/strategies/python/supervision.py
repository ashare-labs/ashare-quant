"""Local process lifetime and output bounds, not memory or hostile-code isolation."""

import os
import threading

from ashare_quant.contracts import DomainError

OUTPUT_LIMIT = 1048576


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
