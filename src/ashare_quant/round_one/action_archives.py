"""Bounded issuer facts and archive-version comparison; no factor or execution feed."""

import argparse
import json
import os
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from .archive_worker import PIN, ROLE, WHEEL
from .independent_queries import dump, reference_snapshot, request, sha
from .independent_repair import capture_window
from .query_compare import expected
from .query_math import Snapshot, compare
from .query_projection import MODEL, project

ARCHIVE_MODEL = "aq.archive-observed-query.v1"


def research_only(product):
    if product.get("role") != ROLE or any(
        product.get(k) is not False for k in ("historical_pit", "strategy_visible", "matching_feed")
    ):
        raise ValueError("ARCHIVE_RESEARCH_CAPABILITY_CHANGED")


def archive_origin(product):
    research_only(product)
    source, version = product["source"], product["archive_version"]
    if not version.get("commit") or version["commit"] != source.get("archive_commit"):
        raise ValueError("ARCHIVE_COMMIT_MISMATCH")
    repository = source["repository_owner"] + "/" + source["repository_name"]
    return dict(
        namespace="ashare-data-dolt-archive",
        dataset_id=repository + ":" + product["product_id"] + ":" + version["commit"],
        manifest_sha256=product["binding_sha256"],
        evidence_kind="owner_data",
        source_refs=[
            source["url"],
            "sha256:" + source["sha256"],
            "dolt-repository:" + repository + ":" + product["product_id"],
        ],
        quality_conclusion="observed archive values; shared repository lineage; no upstream timing or PIT certification",
    )


def archive_projection(product, contract, op, *, model, calendar_exchange=None):
    if model != ARCHIVE_MODEL:
        raise ValueError("ARCHIVE_EXPLICIT_MODEL_REQUIRED")
    origin = archive_origin(product)
    if contract["security"] != product["security"]:
        raise ValueError("ARCHIVE_SECURITY_SCOPE_MISMATCH")
    dates = [row["date"] for row in product["rows"]]
    if not dates or dates != sorted(set(dates)) or dates != contract["expected_dates"]:
        raise ValueError("ARCHIVE_OBSERVED_DATE_SCOPE_MISMATCH")
    p = op["parameters"]
    if p.get("end_date") != contract["end_date"]:
        raise ValueError("ARCHIVE_REQUEST_END_MISMATCH")
    provenance = dict(
        model=model,
        contract=deepcopy(contract),
        archive_version=deepcopy(product["archive_version"]),
        archive_http_capture=deepcopy(product["source"]),
        reference_answers_used=False,
        upstream_capture_time_inferred=False,
        source_revision_inferred=False,
    )
    if product["product_id"] == "C05-archive-daily":
        if (
            contract["kind"] != "archive_daily"
            or op["api"] != "get_price"
            or type(p.get("count")) is not int
            or p["count"] != len(dates)
            or p.get("start_date") is not None
            or p.get("fill_paused") is not False
            or p.get("skip_paused") is not False
        ):
            raise ValueError("ARCHIVE_DAILY_COUNT_SCOPE_MISMATCH")
        bounded_product = deepcopy(product)
        bounded_product.update(start_date=contract["start_date"], end_date=contract["end_date"])
        bounded_op = deepcopy(op)
        del bounded_op["parameters"]["count"]
        bounded_op["parameters"]["start_date"] = contract["start_date"]
        result, table = project(bounded_product, bounded_op, model=MODEL)
        result = result.model_dump(mode="json")
        result.update(origin=origin, request=request(op))
        provenance.update(
            table_projection=table,
            full_session_coverage_certified=False,
            native_fields_preserved=[row["native_fields"] for row in product["rows"]],
            count_semantics="observed response rows; no complete last-session certification",
        )
    elif product["product_id"] == "C05-archive-calendar":
        if (
            contract["kind"] != "archive_calendar"
            or op["api"] != "get_trade_days"
            or set(p) != {"start_date", "end_date"}
            or p["start_date"] != contract["start_date"]
            or not calendar_exchange
            or calendar_exchange != contract["exchange"]
            or calendar_exchange != product["calendar_exchange"]
            or product.get("official_calendar_certified") is not False
            or any(
                row["exchange"] != calendar_exchange or type(row["is_open"]) is not bool
                for row in product["rows"]
            )
        ):
            raise ValueError("ARCHIVE_CALENDAR_SCOPE_MISMATCH")
        result = dict(
            origin=origin,
            request=request(op),
            status="available",
            rows=[dict(date=row["date"]) for row in product["rows"] if row["is_open"]],
            captured_at=product["source"]["captured_end"],
        )
        provenance.update(
            calendar_exchange=calendar_exchange,
            official_calendar_certified=False,
            other_exchange_inference=False,
            comparison_scope="SSE observed open-date labels versus original JQ labels; no exchange equivalence certified",
        )
    else:
        raise ValueError("ARCHIVE_PRODUCT_UNSUPPORTED")
    return Snapshot.model_validate(result), provenance


def archive_version_pair(left, right, snapshots, *, fields):
    if (
        left["product_id"] != right["product_id"]
        or left["security"] != right["security"]
        or left["source"]["repository_owner"] != right["source"]["repository_owner"]
        or left["source"]["repository_name"] != right["source"]["repository_name"]
    ):
        raise ValueError("ARCHIVE_PAIR_SCOPE_MISMATCH")
    for value in (left, right):
        archive_origin(value)
    if left["archive_version"]["commit"] == right["archive_version"]["commit"]:
        raise ValueError("ARCHIVE_DISTINCT_COMMITS_REQUIRED")
    calculation = compare(*snapshots, mode="rows", keys=["date"], fields=fields)
    return dict(
        status="ARCHIVE_VERSIONS_COMPARED_NOT_UPSTREAM_CAPTURES",
        versions=[deepcopy(v["archive_version"]) for v in (left, right)],
        selected_rows_identical=left["rows"] == right["rows"],
        raw_selected_rows=[deepcopy(v["rows"]) for v in (left, right)],
        comparison=calculation,
        original_capture_count=None,
        source_value_revision_proven=False,
        meaning="value differences, if present, require upstream revision attribution; equal values show no selected revision",
    )


def action_receipt(product, contract):
    research_only(product)
    ids = [row["event_id"] for row in product["rows"]]
    if (
        product["product_id"] != "B25-issuer-evidence"
        or product["security"] != contract["security"]
        or set(ids) != set(contract["event_ids"])
        or len(ids) != len(set(ids))
        or product.get("historical_action_chain_complete") is not False
        or product.get("adjustment_factor_certified") is not False
    ):
        raise ValueError("ACTION_EVIDENCE_SCOPE_OR_CAPABILITY_CHANGED")
    for row in product["rows"]:
        if not row.get("citations"):
            raise ValueError("ACTION_DOCUMENT_CITATION_REQUIRED")
        for citation in row["citations"]:
            if (
                citation["document"] not in product["documents"]
                or not citation["pages"]
                or any(type(page) is not int or page < 1 for page in citation["pages"])
            ):
                raise ValueError("ACTION_DOCUMENT_CITATION_REQUIRED")
    return dict(
        status="OWNER_ISSUER_FACTS_RECEIVED",
        facts=deepcopy(product["rows"]),
        documents=deepcopy(product["documents"]),
        source=deepcopy(product["source"]),
        source_ledger_replaced=False,
        adjustment_formula_inferred=False,
        engine_events_emitted=0,
        historical_action_chain_certified=False,
        missing_dates_filled=False,
        investor_entitlement_inferred=False,
    )


def collect(owner_python, binding, wheel):
    result = subprocess.run(
        [
            str(owner_python),
            "-I",
            "-B",
            str(Path(__file__).with_name("archive_worker.py")),
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
        raise ValueError("ARCHIVE_PUBLIC_READ_FAILED:" + result.stderr[-4000:])
    return json.loads(result.stdout)


def run(owner_python, binding, wheel, reference_binding, output):
    output.mkdir(parents=True, exist_ok=False)
    public = collect(owner_python, binding, wheel)
    dump(output / "public-products.json", public)
    products, contracts = public["products"], public["product_contracts"]
    dump(
        output / "B25-issuer-receipt.json",
        action_receipt(products["b25-issuer-evidence"], contracts["B25-issuer-evidence"]),
    )
    bindings = json.loads(reference_binding.read_text())
    inputs_path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(inputs_path.read_text())["cases"]["C05"]
    case = next(c for c in bindings["cases"] if c["case_id"] == "C05")
    for name, digest in case["files"].items():
        if sha(Path(case["folder"]) / name) != digest or inputs["reference_files"][name] != digest:
            raise ValueError("ARCHIVE_REFERENCE_CHANGED")
    projections = []
    for op in inputs["operations"]:
        kind = "daily" if op["api"] == "get_price" else "calendar"
        for label in ("early", "late"):
            name = f"c05-{label}-{kind}"
            product = products[name]
            value, provenance = archive_projection(
                product,
                contracts[product["product_id"]],
                op,
                model=ARCHIVE_MODEL,
                calendar_exchange="SSE" if kind == "calendar" else None,
            )
            projections.append((op, name, value, provenance))
    dump(
        output / "C05-projected.json",
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
    pairs = {}
    for kind in ("daily", "calendar"):
        selected = [v for op, name, v, _ in projections if name.endswith(kind)]
        op = next(op for op, name, _, _ in projections if name.endswith(kind))
        pairs[kind] = archive_version_pair(
            products[f"c05-early-{kind}"],
            products[f"c05-late-{kind}"],
            selected,
            fields=op["parameters"]["fields"] if kind == "daily" else ["date"],
        )
    dump(output / "archive-version-pairs.json", pairs)
    # Owner facts, projections and archive comparisons precede reading JQ answers.
    events = [
        json.loads(line, parse_float=Decimal)
        for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
    ]
    comparisons = []
    for op, name, value, provenance in projections:
        observed = expected(events, op["request_ref_seq"])
        window = capture_window(events, op["request_ref_seq"])
        if op["api"] == "get_price":
            jq = reference_snapshot(observed, op, case)
            fields = op["parameters"]["fields"]
        else:
            jq = Snapshot(
                origin=dict(
                    namespace="joinquant-frozen-reference",
                    dataset_id="C05",
                    manifest_sha256=case["files"]["events.jsonl"],
                    evidence_kind="jq_observation",
                    source_refs=[
                        case["reference_binding"]["run_url"],
                        "sha256:" + case["files"]["events.jsonl"],
                    ],
                    quality_conclusion="frozen original calendar labels; no cross-exchange certification",
                ),
                request=request(op),
                status="available",
                rows=observed["rows"],
            )
            fields = ["date"]
        if window:
            jq = Snapshot.model_validate(
                {**jq.model_dump(mode="json"), "captured_at": window["end"]}
            )
        result = compare(jq, value, mode="freshness", keys=["date"], fields=fields)
        result.update(
            owner_product=name,
            owner_metadata={k: v for k, v in products[name].items() if k != "rows"},
            reference_capture_window=window,
            projection=provenance,
            request_ref_seq=op["request_ref_seq"],
        )
        comparisons.append(result)
    report = dict(
        case_id="C05",
        status="ARCHIVE_QUERY_PORTIONS_COMPARED",
        whole_case_pass=False,
        comparisons=comparisons,
        exact_differences=sum(len(c["comparison"]["differences"]) for c in comparisons),
        original_upstream_capture_versions_known=False,
        source_publication_revision_unknown=True,
    )
    dump(output / "C05.json", report)
    summary = dict(
        schema="aq.action-archive-batch.v1",
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        public_read={k: v for k, v in public.items() if k not in ("products", "product_contracts")},
        reference_comparisons=len(comparisons),
        archive_pair_comparisons=len(pairs),
        exact_reference_differences=report["exact_differences"],
        artifacts={p.name: sha(p) for p in sorted(output.glob("*.json"))},
        input_bindings_sha256=sha(reference_binding),
        query_inputs_sha256=sha(inputs_path),
        calculator_sources={
            n: sha(Path(__file__).with_name(n))
            for n in (
                "archive_worker.py",
                "action_archives.py",
                "query_projection.py",
                "independent_repair.py",
                "independent_queries.py",
                "query_math.py",
                "query_compare.py",
            )
        },
        new_JQ_runs=0,
        new_native_runs=0,
        new_external_requests=0,
    )
    dump(output / "complete.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("owner-python", "binding", "wheel", "reference-binding", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run(args.owner_python, args.binding, args.wheel, args.reference_binding, args.output)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "reference_comparisons",
                    "archive_pair_comparisons",
                    "exact_reference_differences",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
