"""User rule execution against supplied owner components. No substituted market rows."""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.storage.canonical import digest
from ashare_quant.storage.store import Store
from ashare_quant.tasks.worker import work_one
from tests.daily_tasks.conftest import request
from tests.daily_tasks.test_native import complete
from tests.support.daily.executor import Configuration
from tests.support.daily.service import Jobs
from tests.support.legacy_backtests.contracts import Signal
from tests.support.legacy_backtests.journal import Journal, check_decision

EXAMPLES = Path(__file__).parents[2] / "tests/fixtures/research" / "rules"


def register(client, name):
    response = client.post(
        "/api/v1/tasks/daily/strategies", json=json.loads((EXAMPLES / (name + ".json")).read_text())
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_rule(client, version, *, window="w1", cash="10000", key="rules"):
    body = request(client, window=window, key=key)
    body["parameters"] = {
        "template": "registered_rules.v1",
        "strategy_version_id": version["version_id"],
        "initial_cash": cash,
    }
    response = client.post("/api/v1/tasks/daily/jobs", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


@pytest.mark.parametrize(
    "example,window,cash,targets,quantities,expected_cash,final_equity",
    [
        ("hold", "two_day", "10000", [100, 100], [100, 100], ["8735", "8735"], "9981"),
        ("cash", "two_day", "1500", [100, 0], [100, 0], ["235", "1474.754"], "1474.754"),
        (
            "staged",
            "w1",
            "10000",
            [100, 200, 0] + [None] * 7,
            [100, 200] + [0] * 8,
            ["8735", "7484"] + ["9976.5"] * 8,
            "9976.5",
        ),
    ],
)
def test_user_rules_match_manual_account_oracles(
    host, example, window, cash, targets, quantities, expected_cash, final_equity
):
    jobs, client = host
    version = register(client, example)
    job, body = create_rule(client, version, window=window, cash=cash)
    assert client.post("/api/v1/tasks/daily/jobs", json=body).json()["job_id"] == job["job_id"]
    result = complete(jobs, job["job_id"])
    assert result.strategy.model_dump(mode="json") == version
    assert [day.signal.target_shares for day in result.days] == targets
    assert [day.account_at_day_close.quantity for day in result.days] == quantities
    assert [Decimal(day.account_at_day_close.cash) for day in result.days] == list(
        map(Decimal, expected_cash)
    )
    assert Decimal(result.summary.final_equity) == Decimal(final_equity)
    assert result.summary.committed_fill_count == {"hold": 1, "cash": 2, "staged": 3}[example]
    assert result.days[0].account_at_day_close.sellable == 0
    assert result.summary.actual_history_call_count == len(targets)
    assert result.data_identity.original_quality["historical_pit_verified"] is False
    manifest = jobs.check(jobs.repo.row(job["job_id"]))
    assert manifest.strategy == result.strategy
    if example == "staged":
        assert result.days[1].account_at_day_close.sellable == 100
        assert [
            (fill.side, fill.quantity, fill.price) for d in result.days for fill in d.fills
        ] == [("buy", 100, "12.6"), ("buy", 100, "12.46"), ("sell", 200, "12.5")]
        raw = json.loads((jobs.directory(job["job_id"]) / "receipt.json").read_text())
        journal = Journal.model_validate(raw["journal"]["m2_state"])
        original = journal.decisions[0]
        forged_signal = Signal(
            action="target_shares", target_shares=200, reason="rule_matched", rule_index=0
        )
        forged = original.model_copy(
            update={
                "signal": forged_signal,
                "decision_id": digest(
                    {"input": original.economic_input_sha256, "signal": forged_signal}
                ),
            }
        )
        with pytest.raises(DomainError) as error:
            check_decision(forged, journal, journal.initial_account)
        assert error.value.code == "M2_DECISION_SIGNAL_MISMATCH"


def test_rule_version_frozen_across_crash_and_relocation(host, tmp_path):
    jobs, client = host
    version = register(client, "staged")
    baseline = complete(jobs, create_rule(client, version, key="normal")[0]["job_id"])
    job, _ = create_rule(client, version, key="crash")
    identity = jobs.check(jobs.repo.row(job["job_id"]))
    work_one(jobs, job["job_id"], _fault="native_submit:5")
    assert jobs.get(job["job_id"]).status == "failed"
    assert jobs.get(job["job_id"]).can_resume
    # Accepted tasks use frozen strategy bytes, independent of the live registration table.
    with jobs.repo.store.connection(transaction=True) as db:
        db.execute("DELETE FROM registered_strategies WHERE version_id=?", (version["version_id"],))
    target = tmp_path / "moved"
    target.mkdir()
    shutil.move(str(jobs.repo.store.path), target / "renamed.sqlite3")
    shutil.move(str(jobs.root), target / "renamed.sqlite3.tasks")
    shutil.copytree(jobs.config.owner_store, target / "owner")
    resumed = Jobs(Store(target / "renamed.sqlite3"), Configuration(target / "owner"))
    resumed.resume(job["job_id"])
    result = complete(resumed, job["job_id"])
    assert result.summary == baseline.summary and result.account == baseline.account
    assert result.strategy == identity.strategy
    assert resumed.get(job["job_id"]).account_id == job["account_id"]
    assert resumed.get(job["job_id"]).session_sha256 == job["session_sha256"]
    assert len({f.fill_id for day in result.days for f in day.fills}) == 3
    assert result.recovery["native_generation"] == 1


def test_reference_and_frozen_strategy_tampering_are_rejected(host):
    jobs, client = host
    version = register(client, "hold")
    before = client.get("/api/v1/tasks/daily/jobs").json()
    body = request(client)
    body["parameters"] = {
        "template": "registered_rules.v1",
        "strategy_version_id": "0" * 64,
    }
    assert client.post("/api/v1/tasks/daily/jobs", json=body).status_code == 404
    assert client.get("/api/v1/tasks/daily/jobs").json() == before
    job, body = create_rule(client, version)
    frozen = jobs.directory(job["job_id"]) / "strategy.json"
    raw = frozen.read_bytes()
    try:
        value = json.loads(raw)
        value["definition"]["rules"][0]["target_shares"] = 200
        frozen.write_text(json.dumps(value))
        with pytest.raises(DomainError):
            jobs.get(job["job_id"])
    finally:
        frozen.write_bytes(raw)
    changed = json.loads((EXAMPLES / "hold.json").read_text())
    changed["rules"][0]["target_shares"] = 200
    v2 = client.post("/api/v1/tasks/daily/strategies", json=changed).json()
    assert v2["version_id"] != version["version_id"]
    assert jobs.check(jobs.repo.row(job["job_id"])).strategy.version_id == version["version_id"]
    body["parameters"]["strategy_version_id"] = v2["version_id"]
    assert client.post("/api/v1/tasks/daily/jobs", json=body).status_code == 409
