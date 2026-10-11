"""Explicit JQ-shaped table projection over independent owner facts.

This formats missing table cells and paused flags only. It never adjusts prices,
computes factors, fills bars, or modifies the independently preserved owner result.
"""

import argparse
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from .independent_queries import (
    compare_product,
    dump,
    owner_snapshot,
    reference_snapshot,
    request,
    selection,
    sha,
)
from .query_compare import expected
from .query_math import Snapshot, compare

MODEL = "aq.jq-daily-table-projection.v1"
BASE_SEAL_SHA = "c4b68e542e519256c2c52090f51a2941ae184921edf4a13e46d6fdddb7d23b0b"


def project(product, operation, *, model):
    if model != MODEL:
        raise ValueError("QUERY_PROJECTION_MODEL_REQUIRED")
    if product.get("role") != "independent_retrospective_query_research" or any(
        product.get(key) is not False
        for key in ("historical_pit", "strategy_visible", "matching_feed")
    ):
        raise ValueError("QUERY_PROJECTION_OWNER_CAPABILITY_CHANGED")
    original, capabilities = owner_snapshot(product, operation)
    value = original.model_dump(mode="json")
    changes = []
    parameters = operation["parameters"]
    missing = product.get("raw_row_present") is False
    mask = parameters.get("fill_paused") is False and parameters.get("skip_paused") is False
    if missing and mask:
        fact = product.get("pause_fact", {})
        if fact.get("full_day_paused") is not True or fact.get("security") != product["security"]:
            raise ValueError("QUERY_PROJECTION_MISSING_PAUSE_FACT")
        if any(c["state"] == "unsupported" for c in capabilities):
            raise ValueError("QUERY_PROJECTION_CANNOT_MASK_UNSUPPORTED")
        for row in value["rows"]:
            if fact.get("date") != row["date"]:
                raise ValueError("QUERY_PROJECTION_PAUSE_DATE_MISMATCH")
            for field in parameters["fields"]:
                before = row[field]
                after = {"nonfinite": "nan"}
                if before != after:
                    changes.append(
                        dict(
                            date=row["date"],
                            field=field,
                            before=before,
                            after=after,
                            rule="missing_unfilled_table_row",
                        )
                    )
                    row[field] = after
    for row in value["rows"]:
        if type(row.get("paused")) is bool:
            before = row["paused"]
            row["paused"] = "1" if before else "0"
            changes.append(
                dict(
                    date=row["date"],
                    field="paused",
                    before=before,
                    after=row["paused"],
                    rule="numeric_paused_table_column",
                )
            )
    projected = Snapshot.model_validate(value)
    return projected, dict(
        model=MODEL,
        changes=changes,
        owner_rows_unchanged=deepcopy(product["rows"]),
        owner_pause_fact=deepcopy(product.get("pause_fact")),
        owner_field_capabilities=capabilities,
        reference_answers_used=False,
        scope="bounded daily table representation; no new facts or prices",
    )


def sealed_products(run):
    seal_path = run / "complete.json"
    if sha(seal_path) != BASE_SEAL_SHA:
        raise ValueError("QUERY_PROJECTION_BASE_SEAL_CHANGED")
    seal = json.loads(seal_path.read_text())
    for name, digest in seal["artifacts"].items():
        if sha(run / name) != digest:
            raise ValueError("QUERY_PROJECTION_BASE_ARTIFACT_CHANGED:" + name)
    return json.loads((run / "public-products.json").read_text())["products"]


def run(prior_run, bindings_path, output):
    products = sealed_products(prior_run)
    output.mkdir(parents=True, exist_ok=False)
    inputs_path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(inputs_path.read_text())["cases"]
    bindings = json.loads(bindings_path.read_text())
    reports = {}
    for cid in ("B28", "C02"):
        case = next(c for c in bindings["cases"] if c["case_id"] == cid)
        for name, digest in case["files"].items():
            if (
                sha(Path(case["folder"]) / name) != digest
                or inputs[cid]["reference_files"][name] != digest
            ):
                raise ValueError("QUERY_PROJECTION_REFERENCE_CHANGED")
        # All actual projections depend only on owner data and the original request.
        projections = []
        for op in inputs[cid]["operations"]:
            for name in selection(cid, op):
                projected, provenance = project(products[name], op, model=MODEL)
                projections.append((op, name, projected, provenance))
        dump(
            output / f"{cid}-projected.json",
            [
                dict(
                    request=request(op),
                    owner_product=name,
                    value=value.model_dump(mode="json"),
                    provenance=provenance,
                )
                for op, name, value, provenance in projections
            ],
        )
        events = [
            json.loads(line, parse_float=Decimal)
            for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
        ]
        comparisons = []
        for op, name, value, provenance in projections:
            observed = expected(events, op["request_ref_seq"])
            jq = reference_snapshot(observed, op, case)
            after = compare(
                jq,
                value,
                mode="field_coverage" if cid == "C02" else "rows",
                keys=["date"],
                fields=op["parameters"]["fields"],
            )
            before = compare_product(cid, products[name], op, observed, case)
            comparisons.append(
                dict(
                    label=op["label"],
                    owner_product=name,
                    model=MODEL,
                    before=before,
                    after=after,
                    projection=provenance,
                )
            )
        reports[cid] = dict(
            case_id=cid,
            comparisons=comparisons,
            before_differences=sum(
                len(c["before"].get("comparison", {}).get("differences", [])) for c in comparisons
            ),
            after_differences=sum(
                len(c["after"].get("comparison", {}).get("differences", [])) for c in comparisons
            ),
            capability_gaps=sum(
                c["after"]["status"] == "CAPABILITY_GAP_RECORDED" for c in comparisons
            ),
        )
        dump(output / f"{cid}.json", reports[cid])
    result = dict(
        schema="aq.query-projection-replay.v1",
        model=MODEL,
        base_seal_sha256=BASE_SEAL_SHA,
        new_external_requests=0,
        new_owner_reads=0,
        new_JQ_runs=0,
        new_native_runs=0,
        source_sha256=sha(Path(__file__)),
        query_inputs_sha256=sha(inputs_path),
        artifacts={p.name: sha(p) for p in sorted(output.glob("*.json"))},
        cases={
            cid: {k: v for k, v in value.items() if k != "comparisons"}
            for cid, value in reports.items()
        },
    )
    dump(output / "complete.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prior-run", "bindings", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.prior_run, args.bindings, args.output)["cases"]))


if __name__ == "__main__":
    main()
