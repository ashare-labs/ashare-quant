"""Bound read-only discovery without accumulating readers on a stalled volume."""

from concurrent.futures import Future, TimeoutError
from threading import Lock, Thread

from ashare_quant.contracts import DomainError


class DiscoveryRead:
    """One in-flight read, no completed-result cache and no shutdown join.

    A filesystem call cannot safely be cancelled inside Python. The daemon may
    finish after the HTTP deadline; concurrent retries share it while it is
    running. The next request after completion starts a fresh read, so a prior
    release/environment is never silently reused for new discovery.
    """

    def __init__(self, *, timeout=5.0):
        self.timeout = timeout
        self._lock = Lock()
        self._pending = None

    def run(self, operation):
        with self._lock:
            if self._pending is None or self._pending.done():
                future = self._pending = Future()

                def read():
                    try:
                        future.set_result(operation())
                    except BaseException as exc:
                        future.set_exception(exc)

                Thread(target=read, name="catalogue-discovery", daemon=True).start()
            future = self._pending
        try:
            return future.result(timeout=self.timeout)
        except TimeoutError as exc:
            raise DomainError(
                "CATALOGUE_DATA_TIMEOUT",
                "Market metadata did not respond before the discovery deadline; "
                "strategy files remain available. Retry after the data volume responds.",
                503,
                details={"retryable": True, "timeout_seconds": self.timeout,
                         "market_data_verified": False, "read_cancelled": False},
            ) from exc


def discovery_status(error=None):
    return {
        "status": "unavailable" if error else "available",
        "scope": "catalogue_metadata_only",
        "execution_verified": False,
        **({"error": error.body()} if error else {}),
    }
