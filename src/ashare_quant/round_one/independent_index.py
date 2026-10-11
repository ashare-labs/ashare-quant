"""Compare independently read archival candidates, without historical certification."""

import argparse
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

from .independent_queries import dump, request, sha
from .index_worker import PIN, PRODUCTS, ROLE, WHEEL
from .query_compare import expected
from .query_math import Snapshot, compare


def owner_snapshot(product, op, *, allow_uncertified=False):
    if allow_uncertified is not True:
        raise ValueError("INDEX_UNCERTIFIED_OPT_IN_REQUIRED")
    if (
        op["api"] != "get_index_stocks"
        or set(op["parameters"]) != {"index_symbol", "date"}
        or product["security"] != op["parameters"]["index_symbol"]
        or product["as_of_date"] != op["parameters"]["date"]
        or product["product_id"] != "C04-members"
    ):
        raise ValueError("INDEX_REQUEST_SCOPE_MISMATCH")
    if product["role"] != ROLE or any(
        product[k] is not False
        for k in (
            "historical_pit",
            "strategy_visible",
            "matching_feed",
            "official_chain_verified",
            "complete_membership_certified",
            "historical_name_verified",
        )
    ):
        raise ValueError("INDEX_CANDIDATE_CAPABILITY_CHANGED")
    source = product["source"]
    return Snapshot(
        origin=dict(
            namespace="ashare-data-independent-index-candidate",
            dataset_id=product["product_id"] + ":" + source["sha256"],
            manifest_sha256=product["binding_sha256"],
            evidence_kind="owner_data",
            source_refs=[source["url"], "sha256:" + source["sha256"]],
            quality_conclusion=source["quality"] + "; uncertified archival candidate; codes only",
        ),
        request=request(op),
        status="available",
        # Preserve supplied codes and multiplicity. No names or successor substitution.
        rows=[dict(code=row["security"]) for row in product["rows"]],
        captured_at=source.get("captured_end"),
        published_at=source.get("source_publication_time"),
        revision_id=source.get("source_revision"),
    )


def compare_product(product, op, reference, case, *, allow_uncertified=False):
    owner = owner_snapshot(product, op, allow_uncertified=allow_uncertified)
    jq = Snapshot(
        origin=dict(
            namespace="joinquant-frozen-reference",
            dataset_id=case["case_id"],
            manifest_sha256=case["files"]["events.jsonl"],
            evidence_kind="jq_observation",
            source_refs=[
                case["reference_binding"]["run_url"],
                "sha256:" + case["files"]["events.jsonl"],
            ],
            quality_conclusion="frozen original query; historical visibility not certified",
        ),
        request=request(op),
        status="error" if "error_type" in reference else "available",
        rows=reference.get("rows", []),
        reason=json.dumps(reference) if "error_type" in reference else None,
    )
    result = compare(jq, owner, mode="membership", keys=["code"], fields=["code"])
    result.update(
        request_ref_seq=op["request_ref_seq"],
        owner_metadata={k: v for k, v in product.items() if k != "rows"},
        allow_uncertified=True,
        names_compared=False,
        future_identity_rewrites=False,
        query_scope_mapping="same-date code comparison only; not a historical strategy-visible query",
    )
    return result


def collect(owner_python, binding, wheel):
    result = subprocess.run(
        [
            str(owner_python),
            "-I",
            "-B",
            str(Path(__file__).with_name("index_worker.py")),
            str(binding),
            str(wheel),
        ],
        cwd="/",
        env={"PATH": os.defpath, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("INDEX_PUBLIC_READ_FAILED:" + result.stderr[-4000:])
    return json.loads(result.stdout)


def run(owner_python, binding, wheel, reference_binding, output):
    output.mkdir(parents=True, exist_ok=False)
    public = collect(owner_python, binding, wheel)
    dump(output / "public-products.json", public)
    # Public owner products are saved before opening any frozen JQ response.
    bindings = json.loads(reference_binding.read_text())
    inputs_path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(inputs_path.read_text())["cases"]["C04"]
    case = next(c for c in bindings["cases"] if c["case_id"] == "C04")
    for name, digest in case["files"].items():
        if sha(Path(case["folder"]) / name) != digest or inputs["reference_files"][name] != digest:
            raise ValueError("INDEX_REFERENCE_CHANGED")
    events = [
        json.loads(line, parse_float=Decimal)
        for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
    ]
    comparisons = []
    for day, source in PRODUCTS:
        op = next(o for o in inputs["operations"] if o["parameters"]["date"] == day)
        name = day + "-" + source
        value = compare_product(
            public["products"][name],
            op,
            expected(events, op["request_ref_seq"]),
            case,
            allow_uncertified=True,
        )
        value["owner_read_name"] = name
        comparisons.append(value)
    report = dict(
        schema="aq.independent-index-comparison.v1",
        case_id="C04",
        status="UNCERTIFIED_ARCHIVAL_CANDIDATES_COMPARED",
        whole_case_pass=False,
        comparisons=comparisons,
        new_JQ_runs=0,
        new_native_runs=0,
    )
    dump(output / "C04.json", report)
    summary = dict(
        schema="aq.independent-index-batch.v1",
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        public_read={k: v for k, v in public.items() if k != "products"},
        comparisons=[
            dict(owner_read_name=r["owner_read_name"], **r["computation"]) for r in comparisons
        ],
        artifacts={p.name: sha(p) for p in sorted(output.glob("*.json"))},
        input_bindings_sha256=sha(reference_binding),
        query_inputs_sha256=sha(inputs_path),
        calculator_sources={
            n: sha(Path(__file__).with_name(n))
            for n in (
                "independent_index.py",
                "index_worker.py",
                "query_math.py",
                "query_compare.py",
                "independent_queries.py",
            )
        },
    )
    dump(output / "complete.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("owner-python", "binding", "wheel", "reference-binding", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    value = run(args.owner_python, args.binding, args.wheel, args.reference_binding, args.output)
    print(json.dumps(value["comparisons"], ensure_ascii=False))


if __name__ == "__main__":
    main()
