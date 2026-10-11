"""Compare the opt-in simulated net model while retaining the gross baseline."""

from decimal import Decimal
from pathlib import Path

from .compare import read_json
from .dividend_compare import compare as compare_gross


def compare(binding, root):
    report = compare_gross(binding, root)
    result = read_json(Path(root) / "result.json")
    model = result["model"]
    if model["schema"] not in {
        "aq.b26-jq-fixed-dividend-tax.v1",
        "aq.b26-source-premarket-reference.v1",
    }:
        raise ValueError("B26_NET_MODEL_REQUIRED")
    policy = model["tax_compatibility"]
    if policy["schema"] != "aq.jq-fixed-dividend-tax.v1" or policy["rate"] != "0.20":
        raise ValueError("B26_NET_POLICY_MISMATCH")
    transition = result["native_dividend_transitions"][0]
    before, after = transition["before"], transition["after"]
    quantity = before["positions"][0]["quantity"]
    per_share = Decimal(transition["source_gross_per_share"])
    gross = per_share * quantity
    net = gross * (1 - Decimal(policy["rate"]))
    mark = Decimal(before["positions"][0]["last_price"]) - per_share
    if (
        quantity <= 0
        or after["positions"][0]["quantity"] != quantity
        or abs(Decimal(after["cash"]) - Decimal(before["cash"]) - net) > Decimal("1e-9")
        or abs(Decimal(after["positions"][0]["last_price"]) - mark) > Decimal("1e-9")
        or transition["tax_model"] != "jq_fixed_20_percent_simulation"
        or transition["position_owner"] != "FixedTaxStockPosition"
    ):
        raise ValueError("B26_NATIVE_NET_POSTING_PROOF_FAILED")
    proof = dict(
        quantity=quantity,
        source_gross_per_share=str(per_share),
        gross_payable=str(gross),
        model_rate=policy["rate"],
        expected_net=str(net),
        native_delta=transition["actual_cash_delta"],
        native_unrounded_ex_mark=str(mark),
        ledger_owner=result["native_account_owner"],
        no_second_cash_ledger=True,
        exact_jq_rounding_certified=False,
        actual_investor_tax_certified=False,
    )
    for row in report["comparisons"]:
        if row["status"] != "DIFFERENT" or row.get("label") != "day_start":
            continue
        if row["field"] in {
            "/positions/0/last_price",
            "/positions/0/market_value",
            "/account/positions_value",
            "/account/total_value",
        }:
            row.update(
                classification="UNPROVEN_JQ_EX_MARK_ROUNDING",
                explanation="Native raw-prior-close minus gross dividend retains precision; no JQ mark rounding rule has been adopted.",
                evidence=proof,
            )
    report["differences"] = [
        r
        for r in report["comparisons"]
        if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    report["unresolved_count"] = sum(
        r.get("classification") in {None, "REQUIRES_INVESTIGATION"} for r in report["differences"]
    )
    report.update(
        status="CONDITIONAL_NET_DIVIDEND_EXECUTED_MARK_DIFFERENCES_RETAINED",
        posting_evidence=proof,
        gross_baseline_retained="docs/round-one/dividend-results/B26.json",
        compatibility_scope="documented legacy fixed simulation tax; frozen B26 observed account checkpoints",
    )
    if model["schema"] == "aq.b26-source-premarket-reference.v1":
        transitions = result["premarket_reference_transitions"]
        if len(transitions) != 1 or not transitions[0]["native_position_state_unchanged"]:
            raise ValueError("B26_REFERENCE_TRANSITION_REQUIRED")
        report.update(
            status="CONDITIONAL_SOURCE_REFERENCE_COMPARISON_COMPLETED",
            reference_transition=transitions[0],
            premarket_reference_model=model["premarket_reference"],
            premarket_economic_difference_count=sum(
                row["status"] == "DIFFERENT"
                and row.get("label") == "day_start"
                and row.get("field")
                in {
                    "/positions/0/last_price",
                    "/positions/0/market_value",
                    "/account/positions_value",
                    "/account/total_value",
                }
                for row in report["comparisons"]
            ),
        )
    return report
