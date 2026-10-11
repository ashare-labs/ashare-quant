"""Offline attribution from fixed independent products; never produces a data feed."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(root):
    baseline = root / ".verification/round-one/independent-batch1-dev10/source-run"
    seal = json.loads((baseline / "complete.json").read_text())
    if (
        sha(baseline / "complete.json")
        != "c4b68e542e519256c2c52090f51a2941ae184921edf4a13e46d6fdddb7d23b0b"
    ):
        raise ValueError("DIAGNOSIS_BASE_CHANGED")
    for name, digest in seal["artifacts"].items():
        if sha(baseline / name) != digest:
            raise ValueError("DIAGNOSIS_ARTIFACT_CHANGED")
    b25 = json.loads((baseline / "B25.json").read_text())
    hypotheses = []
    for c in b25["comparisons"]:
        fq = c["request"]["parameters"]["fq"]
        for diff in c["comparison"]["differences"]:
            if diff["field"] not in ("open", "high", "low", "close") or fq is None:
                continue
            owner, jq = Decimal(diff["right"]), Decimal(diff["left"])
            rounded = owner.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            hypotheses.append(
                dict(
                    product=c["owner_read_name"],
                    date=diff["key"][0],
                    field=diff["field"],
                    owner=diff["right"],
                    jq_observed=diff["left"],
                    candidate_two_decimal_price=str(rounded),
                    candidate_delta=str(rounded - jq),
                    candidate_equal=rounded == jq,
                )
            )
    b27 = json.loads((baseline / "B27.json").read_text())
    c02 = json.loads((baseline / "C02.json").read_text())
    quantization = []
    for case, data in [("B27", b27), ("C02", c02)]:
        for c in data["comparisons"]:
            for d in c.get("comparison", {}).get("differences", []):
                if d["field"] not in ("volume", "money", "avg"):
                    continue
                quantization.append(
                    dict(
                        case=case,
                        field=d["field"],
                        product=c["owner_read_name"],
                        jq=d["left"],
                        owner=d["right"],
                        delta=d["numeric_delta"],
                        owner_precision=c["owner_product_metadata"]["source"]["precision"],
                        attribution="derived_ratio_not_native_avg"
                        if d["field"] == "avg"
                        else "declared_source_quantization",
                        exact_rounding_algorithm_certified=False,
                    )
                )
    history = json.loads((baseline / "C03.json").read_text())["comparisons"][0]
    with localcontext() as ctx:
        ctx.prec = 50
        post = next(c for c in b25["comparisons"] if c["request"]["parameters"]["fq"] == "post")
        factor = next(d for d in post["comparison"]["differences"] if d["field"] == "factor")
        diagnostic_ratio = Decimal(factor["left"]) / Decimal(factor["right"])
    return dict(
        schema="aq.query-attribution.v1",
        base_seal_sha256=sha(baseline / "complete.json"),
        B25=dict(
            owner_formula="provider factor(day) / provider factor(basis or normalization origin)",
            AQ_adjustment_arithmetic=False,
            owner_prices_only_volume_raw=True,
            two_decimal_rounding_diagnostic=hypotheses,
            two_decimal_rounding_sufficient=all(x["candidate_equal"] for x in hypotheses),
            observed_post_factor_ratio=str(diagnostic_ratio),
            factor_ratio_note="Comparison diagnostic only; never used to scale any owner factor or output",
            exact_action_chain_or_rounding_sequence="UNKNOWN",
        ),
        C02_B27_source_precision=quantization,
        C03=dict(
            earliest=history["computed"],
            shared_rows=history["comparison"]["shared_keys"],
            common_field_differences=history["comparison"]["differences"],
            AD_only_rows=len(history["comparison"]["right_only"]),
            extra_last_date=max(history["comparison"]["right_only"])[0],
            attribution="source coverage boundary; AQ did not omit or clip rows",
            fix="none: do not delete pre-2005 owner rows to imitate narrower JQ coverage",
        ),
        new_external_requests=0,
        new_owner_reads=0,
        new_JQ_runs=0,
        calculations_feed_any_strategy=False,
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    result = analyze(root)
    with (root / "docs/round-one/query-attribution-dev11.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            dict(
                B25_two_decimal_rounding_sufficient=result["B25"][
                    "two_decimal_rounding_sufficient"
                ],
                C03_AD_only=result["C03"]["AD_only_rows"],
            )
        )
    )
