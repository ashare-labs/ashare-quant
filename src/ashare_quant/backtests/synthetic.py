"""Scripted test double. Deliberately contains no strategy, matching or accounting logic."""

import hashlib
import json
from importlib.resources import files

from ashare_quant.contracts import DomainError, EngineIdentity, EngineStep

FIXTURE_IDENTITY = EngineIdentity(
    engine_id="fixture-replay-v1", version="1", source_commit=None, is_test_double=True,
)


def fixture_bytes(name: str) -> bytes:
    if name not in specifications():
        raise DomainError("SCENARIO_NOT_FOUND", name, 404)
    return files("ashare_quant").joinpath("fixtures", f"{name}.json").read_bytes()


def specifications() -> dict:
    return json.loads(files("ashare_quant").joinpath("fixtures", "specifications.json").read_text())


class FixtureReplayAdapter:
    identity = FIXTURE_IDENTITY

    def __init__(self, name: str, expected_sha256: str):
        raw = fixture_bytes(name)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        if self.sha256 != expected_sha256:
            raise DomainError("FIXTURE_CHANGED", "Pinned event trace differs from installed package")
        self.steps = [EngineStep.model_validate(event) for event in json.loads(raw)]

    def next_step(self, checkpoint: dict | None) -> EngineStep | None:
        offset = 0
        if checkpoint:
            if checkpoint.get("trace_id") != self.sha256:
                raise DomainError("CHECKPOINT_MISMATCH", "Checkpoint belongs to another trace")
            offset = checkpoint["next_index"]
            if type(offset) is not int or not 0 <= offset <= len(self.steps):
                raise DomainError("CHECKPOINT_INVALID", "Checkpoint offset is out of range")
        if offset == len(self.steps):
            return None
        # This is a transport checkpoint, not a reconstructed account or engine state.
        return self.steps[offset].model_copy(update={
            "checkpoint": {"trace_id": self.sha256, "next_index": offset + 1},
        })
