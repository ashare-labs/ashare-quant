"""R1 independent repro plus missing/regressed/corrupt phase anchors (fixtures)."""

import json

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import atomic, regular
from ashare_quant.paper.service import read_checkpoint
from rqprobe.checkpoint import AtomicFile

from .test_online import closing, finish, prepare, setup, start


@pytest.mark.parametrize("index", [0, 1])
def test_missing_working_checkpoint_restores_published_open_without_new_trade(tmp_path, index):
    service, session = setup(tmp_path)
    parent = None
    if index:
        prior = prepare(service, session)
        start(service, prior)
        parent = finish(service, prior)["checkpoint_sha256"]
    row = prepare(service, session, index=index, targets=(100, 0), parent=parent)
    opened = start(service, row)
    (tmp_path / row["day"] / "native.chk").unlink()
    service.deliver_fixture("closing", closing(row))
    closed = service.advance(row["day"], "closing", fault="inside_native_trade")
    assert closed["native"]["trades"] == opened["native"]["trades"]
    assert service.report(row["day"], "opening") == opened


@pytest.mark.parametrize("damage", ["cursor", "prior_day", "trade", "opening_copy"])
def test_published_open_rejects_rollback_or_tamper_before_native(tmp_path, damage):
    service, session = setup(tmp_path)
    prior = prepare(service, session)
    start(service, prior)
    parent = finish(service, prior)["checkpoint_sha256"]
    row = prepare(service, session, 1, targets=(0, 100), parent=parent)
    start(service, row)
    service.deliver_fixture("closing", closing(row))
    root = tmp_path / row["day"]
    identity = service._load()[0]["identity"]
    if damage == "opening_copy":
        (root / "native.chk").unlink()
        (root / "opening.chk").write_bytes(b"corrupt published anchor")
    elif damage == "prior_day":
        atomic(root / "native.chk", regular(tmp_path / prior["day"] / "closing.chk"))
    else:
        payload, _ = read_checkpoint(root / "native.chk", identity)
        if damage == "cursor":
            payload["states"]["event_source"] = json.dumps({"cursor": 5}).encode()
        else:
            state = json.loads(payload["states"]["mod_paper"])
            state["trades"] = []
            payload["states"]["mod_paper"] = json.dumps(state).encode()
        AtomicFile(root / "native.chk", identity).commit(payload, 6)

    def no_spawn(*args, **kwargs):
        pytest.fail("invalid published-open anchor reached native process")

    service._spawn = no_spawn
    with pytest.raises(DomainError):
        service.advance(row["day"], "closing")
    assert service._load()[0]["head"] == parent
