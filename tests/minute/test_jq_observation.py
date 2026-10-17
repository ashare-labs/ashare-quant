"""Observer invariance and audits of optionally supplied, real platform captures."""

import ast
import copy
import json
import os
from pathlib import Path

import pytest

from tests.fixtures.minute_reference.build_scripts import NAMES, ROOT, generate
from tests.fixtures.minute_reference.compare import (
    aq_observations,
    audit_jq,
    compare_case,
    read_rows,
    validate_rows,
)


class RecoverBusiness(ast.NodeTransformer):
    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            if node.value.func.id == "_observe":
                return ast.Assert(test=self.visit(node.value.args[2]), msg=None)
        return self.generic_visit(node)

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "_submit":
            node.func = ast.Name(id=node.args[1].value, ctx=ast.Load())
            node.args = node.args[2:]
        elif isinstance(node.func, ast.Name) and node.func.id == "_history":
            node.func = ast.Name(id="history", ctx=ast.Load())
            node.args = node.args[1:]
        return node


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("platform", ["aq", "jq"])
def test_observer_preserves_every_original_business_function(name, platform):
    original = ast.parse((ROOT / "examples/minute_research" / f"{name}.py").read_text())
    generated = generate(name, platform)
    # The hosted Python3 runtime accepts these scripts; guard syntax compatibility too.
    actual = {
        f.name: f
        for f in ast.parse(generated, feature_version=(3, 6)).body
        if isinstance(f, ast.FunctionDef)
    }
    for expected in original.body:
        if not isinstance(expected, ast.FunctionDef):
            continue
        f = actual[expected.name]
        if f.name == "initialize":
            f.body = f.body[-len(expected.body) - 1 : -1]  # settings prefix and identity suffix
        else:
            assert isinstance(f.body[1], ast.Try) and not f.body[1].handlers
            f.body = f.body[1].body  # callback entry/exit observations
        recovered = RecoverBusiness().visit(f)
        assert ast.dump(recovered) == ast.dump(expected), expected.name


@pytest.fixture
def actual_capture():
    root = os.environ.get("AQ_JQ_MINUTE_EVIDENCE")
    if not root:
        pytest.skip(
            "Real JQ capture is external evidence; never substitute synthetic platform output"
        )
    return Path(root)


@pytest.mark.parametrize("name", NAMES)
def test_real_jq_full_sequence_source_plans_and_economics(actual_capture, name):
    jq = read_rows(actual_capture / f"{name}.logs.jsonl")
    native = actual_capture / "aq-candidate" / name
    result = compare_case(
        name,
        aq_observations(native / "events.jsonl"),
        jq,
        json.loads((native / "result.json").read_text()),
        native / "input.json",
    )
    assert result["unexplained_difference"] == "0"
    assert result["strict_equivalence"] is False
    assert result["historical_pit_verified"] is False


@pytest.mark.parametrize(
    "mutation", ["gap", "duplicate", "tail", "source", "settings", "clock", "equity"]
)
def test_rejects_incomplete_or_altered_platform_observations(actual_capture, mutation):
    rows = copy.deepcopy(read_rows(actual_capture / "visible_momentum.logs.jsonl"))
    if mutation == "gap":
        rows.pop(3)
    elif mutation == "duplicate":
        rows.insert(3, copy.deepcopy(rows[3]))
    elif mutation == "tail":
        rows.pop()
    elif mutation == "source":
        rows[0]["source_sha256"] = "0" * 64
    elif mutation == "settings":
        rows[0]["run_params"]["frequency"] = "daily"
    elif mutation == "clock":
        rows[5]["time"] = "2026-10-08 09:50:00"
    elif mutation == "equity":
        rows[-2]["account"]["total_value"] += 1
    with pytest.raises(ValueError):
        validate_rows(rows, "visible_momentum", "jq")


@pytest.mark.parametrize("mutation", ["fee", "position", "cash", "fill", "revised_trade", "t1"])
def test_rejects_inconsistent_real_order_fill_and_account_chain(actual_capture, mutation):
    rows = copy.deepcopy(read_rows(actual_capture / "visible_momentum.logs.jsonl"))
    order = next(r["order"] for r in rows if r["kind"] == "order_return")
    snapshots = [r for r in rows if r["kind"] == "snapshot" and r["trades"]]
    if mutation == "fee":
        order["commission"] += 1
    elif mutation == "position":
        snapshots[0]["positions"][0]["total_amount"] -= 100
    elif mutation == "cash":
        snapshots[0]["account"]["available_cash"] += 1
    elif mutation == "fill":
        snapshots[0]["trades"][0]["price"] += 0.01
    elif mutation == "revised_trade":
        snapshots[1]["trades"][0]["amount"] -= 100
    elif mutation == "t1":
        snapshots[0]["positions"][0]["closeable_amount"] = 100
    with pytest.raises(ValueError):
        audit_jq(rows)
