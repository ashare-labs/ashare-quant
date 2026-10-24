"""Product parameters must not inherit the acceptance example's tiny portfolio."""

import pytest
from pydantic import ValidationError

from ashare_quant.tasks.policy import ServicePolicy
from tests.support.daily.executor import Configuration
from tests.support.daily.service import Jobs
from tests.support.legacy_backtests.contracts import Signal, StrategyDefinition
from tests.support.legacy_backtests.parameter_policy import (
    BandParameters,
    RegisteredPythonParameters,
)
from tests.support.legacy_strategies.rules import DecisionRule, RuleCondition


@pytest.mark.parametrize("quantity", [300, 1000, 100000])
def test_whole_lot_targets_reach_every_decision_contract(quantity):
    assert StrategyDefinition(template="buy_hold.v1", target_shares=quantity).target_shares == quantity
    assert BandParameters(target_shares=quantity, initial_cash="30000").definition().target_shares == quantity
    assert DecisionRule(rule_id="buy", when_all=(), target_shares=quantity).target_shares == quantity
    assert Signal(action="target_shares", target_shares=quantity, reason="python_target").target_shares == quantity


@pytest.mark.parametrize("quantity", [True, 300.0, -100, 350, 2**53 + 100])
def test_non_lots_and_inexact_quantities_still_refuse(quantity):
    for model, extra in [(BandParameters, {}), (DecisionRule, dict(rule_id="buy", when_all=())),
                         (Signal, dict(action="target_shares", reason="python_target"))]:
        with pytest.raises(ValidationError):
            model(target_shares=quantity, **extra)


@pytest.mark.parametrize("cash", ["0.01", "500", "30000"])
def test_positive_cash_is_a_user_parameter(cash):
    assert BandParameters(initial_cash=cash).initial_cash == cash
    assert RegisteredPythonParameters(strategy_version_id="a" * 64, initial_cash=cash).initial_cash == cash


@pytest.mark.parametrize("cash", ["0", "-1", "NaN", "1e6"])
def test_cash_shape_and_positivity_still_apply(cash):
    with pytest.raises(ValidationError):
        BandParameters(initial_cash=cash)


def test_only_effective_attempt_budgets_are_configured_and_reported(store):
    policy = ServicePolicy(execution_seconds=180, cpu_seconds=180, preparation_seconds=90, output_bytes=2**21)
    jobs = Jobs(store, Configuration(None, policy))
    caps = jobs.catalogue().python_strategies
    assert (caps.attempt_seconds, caps.output_bytes) == (180, 2**21)
    assert not {"module_load_seconds", "callback_seconds"} & policy.model_dump().keys()
    assert ServicePolicy.from_frozen({**policy.model_dump(), "callback_seconds": 5,
                                      "module_load_seconds": 5}) == policy
    for key in ("callback_seconds", "module_load_seconds"):
        with pytest.raises(ValidationError):
            ServicePolicy(**{key: 5})


def test_model_thresholds_do_not_impose_sample_cash_ceiling():
    assert RuleCondition(field="prior_cash", operator="gt", value="1000001").value == "1000001"
    assert BandParameters(enter_below="1000001", exit_above="2000000").definition().enter_below == "1000001"


@pytest.mark.parametrize("kwargs", [{"execution_seconds": 0}, {"callback_seconds": True},
                                   {"max_attempts": -1}, {"admission_wait_seconds": float("inf")}])
def test_resource_budgets_remain_positive_finite(kwargs):
    with pytest.raises(ValidationError):
        ServicePolicy(**kwargs)


@pytest.mark.parametrize("invalid_lease", [False, True])
def test_runner_watchdog_uses_configured_budget_and_preserves_lease_fence(tmp_path, monkeypatch, invalid_lease):
    runner = vars(__import__("ashare_quant.tasks.control", fromlist=["watch_control"]))
    calls = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, *_):
            calls.append(1)
            return self
        def fetchone(self):
            return ("running", "wrong" if invalid_lease else "token", 1000, None)
    def exit_process(code):
        raise SystemExit(code)
    times = iter([0, 121, 181])
    monkeypatch.setattr(runner["sqlite3"], "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(runner["time"], "monotonic", lambda: next(times))
    monkeypatch.setattr(runner["time"], "time", lambda: 100)
    monkeypatch.setattr(runner["time"], "sleep", lambda _: None)
    monkeypatch.setattr(runner["os"], "_exit", exit_process)
    with pytest.raises(SystemExit) as error:
        runner["watch_control"](dict(database=str(tmp_path / "control.sqlite3"), job_id="job", token="token"), 180)
    assert error.value.code == (74 if invalid_lease else 75)
    assert len(calls) == (1 if invalid_lease else 2)
