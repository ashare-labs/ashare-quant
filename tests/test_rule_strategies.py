"""Public rule registration and deterministic decisions; no market or native coverage."""

import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_quant.api import create_app
from ashare_quant.contracts import DomainError
from ashare_quant.m2.common import canonical
from ashare_quant.m2.parameter_policy import parse_parameters
from ashare_quant.m2.registry import StrategyRegistry
from ashare_quant.m2.rules import RuleStrategyDefinition
from ashare_quant.m2.strategy import evaluate


def definition():
    return {
        "template": "decision_rules.v1",
        "rules": [
            {
                "rule_id": "buy",
                "when_all": [
                    {"field": "previous_close", "operator": "le", "value": "12.5000"},
                    {"field": "prior_quantity", "operator": "eq", "value": "0"},
                ],
                "target_shares": 100,
            },
            {
                "rule_id": "exit",
                "when_all": [
                    {"field": "day_index", "operator": "ge", "value": "3"},
                ],
                "target_shares": 0,
            },
        ],
    }


@pytest.mark.parametrize(
    "operator,values",
    [
        ("lt", (True, False, False)),
        ("le", (True, True, False)),
        ("eq", (False, True, False)),
        ("ne", (True, False, True)),
        ("ge", (False, True, True)),
        ("gt", (False, False, True)),
    ],
)
def test_decimal_comparison_boundaries(operator, values):
    d = RuleStrategyDefinition(
        rules=[
            {
                "rule_id": "threshold",
                "when_all": [
                    {
                        "field": "previous_close",
                        "operator": operator,
                        "value": "12.50",
                    }
                ],
                "target_shares": 100,
            }
        ]
    )
    for price, expected in zip(("12.49999999", "12.5", "12.50000001"), values, strict=True):
        s = evaluate(d, previous_close=price, prior_cash="10000", prior_quantity=0, day_index=0)
        assert (s.action == "target_shares") is expected
        assert s.rule_index == (0 if expected else None)


def test_order_and_all_conditions_and_fallback():
    d = RuleStrategyDefinition.model_validate(definition())
    assert (
        evaluate(
            d, previous_close="12.4", prior_cash="10000", prior_quantity=0, day_index=3
        ).rule_index
        == 0
    )
    s = evaluate(d, previous_close="12.4", prior_cash="10000", prior_quantity=100, day_index=2)
    assert s.action == "no_change" and s.reason == "rules_no_match"
    assert (
        evaluate(
            d, previous_close="12.4", prior_cash="10000", prior_quantity=100, day_index=3
        ).target_shares
        == 0
    )


@pytest.mark.parametrize(
    "defect",
    [
        "code",
        "today",
        "multi",
        "minute",
        "recursive",
        "nan",
        "scientific",
        "whitespace",
        "number",
        "too_large",
        "fractional_day",
        "boolean_target",
        "float_target",
        "negative_target",
        "too_many_rules",
        "too_many_conditions",
        "duplicate_id",
        "early_unconditional",
    ],
)
def test_unsupported_programs_are_rejected(defect):
    d = definition()
    c = d["rules"][0]["when_all"][0]
    if defect == "code":
        d["source"] = "import os"
    elif defect == "today":
        c["field"] = "current_close"
    elif defect == "multi":
        d["securities"] = ["600000.XSHG", "000001.XSHE"]
    elif defect == "minute":
        d["frequency"] = "1m"
    elif defect == "recursive":
        c["expression"] = {"recursive": []}
    elif defect in {"nan", "scientific", "whitespace", "number", "too_large"}:
        c["value"] = {
            "nan": "NaN",
            "scientific": "1e3",
            "whitespace": " 12.5 ",
            "number": 12.5,
            "too_large": "1000001",
        }[defect]
    elif defect == "fractional_day":
        c.update(field="day_index", value="1.5")
    elif defect in {"boolean_target", "float_target", "negative_target"}:
        d["rules"][0]["target_shares"] = {
            "boolean_target": False,
            "float_target": 100.0,
            "negative_target": -100,
        }[defect]
    elif defect == "too_many_rules":
        d["rules"] = [d["rules"][0]] * 17
    elif defect == "too_many_conditions":
        d["rules"][0]["when_all"] = [c] * 9
    elif defect == "duplicate_id":
        d["rules"][1]["rule_id"] = "buy"
    else:
        d["rules"][0]["when_all"] = []
    with pytest.raises(ValidationError):
        RuleStrategyDefinition.model_validate(d)


def test_registry_canonical_versions_are_immutable(store):
    registry = StrategyRegistry(store)
    original = RuleStrategyDefinition.model_validate(definition())
    a = registry.register(original)
    same = definition()
    same["rules"][0]["when_all"][0]["value"] = "12.5"
    assert registry.register(RuleStrategyDefinition.model_validate(same)) == a
    changed = copy.deepcopy(same)
    changed["rules"][0]["target_shares"] = 200
    b = registry.register(RuleStrategyDefinition.model_validate(changed))
    assert b.version_id != a.version_id and b.parameter_sha256 != a.parameter_sha256
    assert registry.get(a.version_id) == a
    tampered = a.model_copy(update={"definition": b.definition})
    with store.connection(transaction=True) as db:
        db.execute(
            "UPDATE m2_strategies SET body=? WHERE version_id=?",
            (canonical(tampered), a.version_id),
        )
    with pytest.raises(DomainError) as error:
        registry.get(a.version_id)
    assert error.value.code == "M2_REGISTRY_CORRUPT"


def test_api_registration_needs_no_data_or_engine_and_does_not_create_jobs(store, monkeypatch):
    monkeypatch.delenv("ASHARE_QUANT_M2_STORE", raising=False)
    with TestClient(create_app(str(store.path))) as client:
        posted = client.post("/api/v1/m2/strategies", json=definition())
        assert posted.status_code == 201, posted.text
        version = posted.json()
        assert client.post("/api/v1/m2/strategies", json=definition()).json() == version
        assert client.get("/api/v1/m2/strategies/" + version["version_id"]).json() == version
        assert client.get("/api/v1/m2/strategies").json() == [version]
        assert client.get("/api/v1/m2/jobs").json() == {"items": []}
        assert client.get("/api/v1/m2/strategies/" + "0" * 64).status_code == 404
        assert client.get("/api/v1/m2/strategies?code=1").status_code == 422
        for field in ("source", "path", "frequency", "securities"):
            assert (
                client.post("/api/v1/m2/strategies", json=definition() | {field: "bad"}).status_code
                == 422
            )
        schema = client.get("/api/v1/m2/openapi.json").json()
        assert "/api/v1/m2/strategies" in schema["paths"]
        c = client.get("/api/v1/m2/jobs/catalogue").json()
        assert c["contract_version"] == "m2-jobs.v6"
        assert c["rule_strategies"]["code_execution"] is False


def test_job_reference_has_no_inline_override():
    params = dict(
        template="registered_rules.v1", strategy_version_id="a" * 64, initial_cash="5000.00"
    )
    assert parse_parameters(params).initial_cash == "5000"
    for extra in (
        {"rules": []},
        {"source": "pass"},
        {"frequency": "1m"},
        {"security": "000001.XSHE"},
    ):
        with pytest.raises(ValidationError):
            parse_parameters(params | extra)
    assert Decimal(parse_parameters(params).initial_cash) == 5000
