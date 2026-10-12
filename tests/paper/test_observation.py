"""Synthetic candidates test control gates, not actual forward account days."""

import copy
import json
import multiprocessing
import os
import signal
from datetime import datetime, timezone

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.m2_jobs.files import digest
from ashare_quant.paper import observation


@pytest.fixture
def packet(monkeypatch):
    monkeypatch.setattr(observation, "now", lambda: datetime(2026, 10, 12, 15, tzinfo=timezone.utc))
    return {
        "schema": "ad-current-daily-observation.v1",
        "signal_day": "2026-10-12",
        "completed_at": "2026-10-12T14:00:00+00:00",
        "evidence_kind": "live_local_capture",
        "execution_permission": False,
        "signal_ready": True,
        "revisions": [],
        "calendar": {"next_open": "2026-10-13", "open_days": ["2026-10-12", "2026-10-13"]},
        "securities": {
            s: {"rows": [{"close": p}] * 120}
            for s, p in zip(
                ("600000.XSHG", "600036.XSHG", "601318.XSHG"), ("10", "40", "50"), strict=True
            )
        },
    }


def freeze(root, packet):
    return observation.freeze(root, packet, digest(packet), "a" * 64)


def test_next_day_candidate_is_not_an_account_or_order(packet, tmp_path):
    value = freeze(tmp_path, packet)
    assert value["signal"]["targets"] == {
        "600000.XSHG": 32600,
        "600036.XSHG": 8100,
        "601318.XSHG": 6500,
    }
    assert value["earliest_simulated_match"] == "2026-10-13T09:30:00+08:00"
    assert not value["execution_admitted"] and not value["candidate_is_order"]
    assert value["account"]["cash"] is None and value["account"]["positions"] is None
    result = observation.status(tmp_path)
    assert result["status"] == "blocked" and result["native_accounts_created"] == 0
    assert not list(tmp_path.rglob("*.chk")) and not list(tmp_path.rglob("*.sqlite3"))


def test_repeat_after_deadline_returns_same_immutable_candidate(packet, tmp_path, monkeypatch):
    first = freeze(tmp_path, packet)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    monkeypatch.setattr(observation, "now", lambda: datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert freeze(tmp_path, packet) == first
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert observation.status(tmp_path)["candidates"][0]["current_phase"] == "missed_window"


def test_new_candidate_cannot_backdate_missed_window(packet, tmp_path, monkeypatch):
    monkeypatch.setattr(observation, "now", lambda: datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
    with pytest.raises(DomainError) as error:
        freeze(tmp_path, packet)
    assert error.value.code == "PAPER_OBSERVATION_MISSED_WINDOW"
    assert not list(tmp_path.rglob("candidates/*.json"))


@pytest.mark.parametrize(
    "mutation,code",
    [
        ({"signal_ready": False, "revisions": ["changed"]}, "PAPER_OBSERVATION_REVISION"),
        ({"completed_at": "2026-10-14T14:00:00+00:00"}, "PAPER_OBSERVATION_FUTURE"),
        ({"execution_permission": True}, "PAPER_OBSERVATION_IDENTITY"),
        ({"evidence_kind": "historical_research"}, "PAPER_OBSERVATION_IDENTITY"),
    ],
)
def test_revisions_future_and_wrong_evidence_block(packet, tmp_path, mutation, code):
    packet.update(mutation)
    with pytest.raises(DomainError) as error:
        freeze(tmp_path, packet)
    assert error.value.code == code


def test_changed_economic_input_cannot_replace_frozen_day(packet, tmp_path):
    first = freeze(tmp_path, packet)
    changed = copy.deepcopy(packet)
    changed["securities"]["600000.XSHG"]["rows"][-1]["close"] = "11"
    with pytest.raises(DomainError) as error:
        freeze(tmp_path, changed)
    assert error.value.code == "PAPER_OBSERVATION_IDEMPOTENCY_CONFLICT"
    assert observation.status(tmp_path)["candidates"][0]["signal"] == first["signal"]


def test_same_economic_delivery_at_a_new_receive_time_is_idempotent(packet, tmp_path):
    first = freeze(tmp_path, packet)
    packet["completed_at"] = "2026-10-12T14:30:00+00:00"
    assert freeze(tmp_path, packet) == first


def test_reader_change_cannot_continue_same_plan(packet, tmp_path):
    freeze(tmp_path, packet)
    with pytest.raises(DomainError) as error:
        observation.freeze(tmp_path, packet, digest(packet), "b" * 64)
    assert error.value.code == "PAPER_OBSERVATION_PLAN_CHANGED"


def test_receipt_tampering_is_visible(packet, tmp_path):
    freeze(tmp_path, packet)
    path = next((tmp_path / "receipts").glob("*.json"))
    body = json.loads(path.read_bytes())
    body["signal"]["targets"]["600000.XSHG"] += 100
    path.write_text(json.dumps(body))
    with pytest.raises(DomainError) as error:
        observation.status(tmp_path)
    assert error.value.code == "PAPER_OBSERVATION_CORRUPT"


def test_sigkill_after_commit_retry_has_one_candidate(packet, tmp_path):
    def worker():
        original = observation.atomic

        def killed(path, content):
            original(path, content)
            if path.parent.name == "candidates":
                os.kill(os.getpid(), signal.SIGKILL)

        observation.atomic = killed
        freeze(tmp_path, packet)

    process = multiprocessing.get_context("fork").Process(target=worker)
    process.start()
    process.join(10)
    assert process.exitcode == -signal.SIGKILL
    old = observation.status(tmp_path)["candidates"][0]
    result = freeze(tmp_path, packet)
    assert result["created_at"] == old["created_at"]
    assert len(list((tmp_path / "candidates").glob("*.json"))) == 1
    assert observation.status(tmp_path)["native_orders_submitted"] == 0
