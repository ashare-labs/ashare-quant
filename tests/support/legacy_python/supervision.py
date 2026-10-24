"""Retired Python phase protocol, preserved only for historical tests."""

import json
import time

from ashare_quant.contracts import DomainError

PHASE_SECONDS = 5


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
