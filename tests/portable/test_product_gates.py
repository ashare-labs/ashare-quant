"""Existing authorized owner data, larger targets and same-account recovery."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ashare_quant.api import create_app
from ashare_quant.m2.rules import DecisionRule, RuleStrategyDefinition
from ashare_quant.m2_jobs.executor import Configuration
from ashare_quant.m2_jobs.files import sha
from ashare_quant.m2_jobs.policy import ServicePolicy
from ashare_quant.m2_jobs.worker import work_one
from ashare_quant.python_strategies.spec import PythonDescriptor

from .conftest import create
from .test_native import complete


@pytest.mark.parametrize("kind", ["template", "rules", "python"])
@pytest.mark.parametrize("cash,filled", [("30000", True), ("500", False)])
def test_larger_target_and_cash_use_native_book(host, tmp_path, kind, cash, filled):
    jobs, client = host
    parameters = dict(template="previous_close_band.v1", target_shares=300,
                      enter_below="100", exit_above="101", initial_cash=cash)
    if kind == "rules":
        version = jobs.register_strategy(RuleStrategyDefinition(rules=(
            DecisionRule(rule_id="hold", when_all=(), target_shares=300),)))
        parameters = dict(template="registered_rules.v1", strategy_version_id=version.version_id, initial_cash=cash)
    elif kind == "python":
        source = tmp_path / "larger.py"
        source.write_text("from ashare_quant.strategy_api import TargetShares\n"
                          "def on_decision(context, data, parameters):\n    return TargetShares(300)\n")
        version = jobs.register_python(source, PythonDescriptor())
        parameters = dict(template="registered_python.v1", strategy_version_id=version.version_id, initial_cash=cash)
    result = complete(jobs, create(client, "m2a", parameters=parameters)["job_id"])
    assert [d.signal.target_shares for d in result.days] == [300, 300]
    assert result.summary.committed_fill_count == (1 if filled else 0)
    assert Decimal(result.summary.final_cash) == Decimal("26215" if filled else "500")
    assert result.days[-1].account_at_day_close.quantity == (300 if filled else 0)
    if not filled:
        assert any(day.native_rejections for day in result.days)


def test_explicit_resume_renews_budget_without_replaying_fills(owner_store, tmp_path):
    owner, dataset = owner_store
    app = create_app(str(tmp_path / "control.sqlite3"),
                     m2_jobs_configuration=Configuration(owner, ServicePolicy(max_attempts=1)))
    jobs = app.state.m2_jobs
    with TestClient(app) as client:
        assert client.post("/api/v1/m2/data/bind", json={"dataset_id": dataset}).status_code == 200
        job_id = create(client, "m2a")["job_id"]
        def cancel_after_publish(stage, *_):
            if stage == "after_publish":
                jobs.cancel(job_id)
        work_one(jobs, job_id, _stage=cancel_after_publish)
        before = jobs.get(job_id)
        checkpoint = sha((jobs.directory(job_id) / "native.chk").read_bytes())
        assert before.status == "cancelled" and before.attempt_count == 1 and before.can_resume
        assert client.post(f"/api/v1/m2/jobs/{job_id}/resume", json={}).status_code == 200
        result = complete(jobs, job_id)
        assert jobs.get(job_id).attempt_count == 2
        assert jobs.get(job_id).account_id == before.account_id
        assert sha((jobs.directory(job_id) / "native.chk").read_bytes()) == checkpoint
        assert result.recovery["completion"]["native_run"] is False
        # The default band enters on day 1 and exits on day 2: two unique fills.
        assert result.summary.committed_fill_count == 2
        assert Decimal(result.summary.final_cash) == Decimal("9974.754")
