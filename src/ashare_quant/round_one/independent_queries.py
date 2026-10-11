"""Bounded retrospective comparison of independent AD products and frozen JQ queries.

No collection, price adjustment, missing-row filling, execution or PIT admission.
"""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .independent_worker import PIN, WHEEL
from .query_compare import expected
from .query_math import Snapshot, compare, number

CASES = ("B27", "C02", "B25", "B28", "C03")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def request(op):
    return {k: op[k] for k in ("api", "parameters", "as_of")}


def daily_date(value):
    # Frozen JQ daily index is a naive midnight label, not a publication instant.
    if not isinstance(value, str):
        raise ValueError("INDEPENDENT_DAILY_LABEL_REQUIRED")
    parsed = datetime.fromisoformat(value) if len(value) > 10 else None
    if parsed and (parsed.hour or parsed.minute or parsed.second or parsed.microsecond):
        raise ValueError("INDEPENDENT_NON_DAILY_LABEL")
    return date.fromisoformat(value[:10]).isoformat()


def selection(cid, op):
    if op["api"] != "get_price":
        return []
    p = op["parameters"]
    if cid in ("B27", "C02"):
        if p["frequency"] != "daily":
            return []
        prefix = "B27-daily" if cid == "B27" else "C02-fields"
        return [prefix + "-" + s for s in ("sina-pingan", "eastmoney-pingan")]
    if cid == "C03":
        return ["C03-history-sina-pingan"]
    if cid == "B28":
        return ["B28-" + {"filled": "filled", "nan_fill": "nan", "skipped": "skipped"}[op["label"]]]
    names = [f"B25-{op['as_of'][:10]}-{p['fq']}"]
    if p["fq"] is None:
        names += ["B25-raw-eastmoney-pingan"]
    return names


def validate_scope(product, op):
    p = op["parameters"]
    if (
        op["api"] != "get_price"
        or p["frequency"] != "daily"
        or product["security"] != p["security"]
    ):
        raise ValueError("INDEPENDENT_REQUEST_SCOPE_MISMATCH")
    end = daily_date(p["end_date"][:10])
    start = p.get("start_date", end)
    if "count" in p and (p["count"] != 1 or product.get("start_date") != end):
        raise ValueError("INDEPENDENT_COUNT_SCOPE_MISMATCH")
    if product["product_id"] == "B28-pause":
        if start != "2016-01-04" or end != start or p.get("fq") is not None:
            raise ValueError("INDEPENDENT_PAUSE_DATE_MISMATCH")
        for key in ("skip_paused", "fill_paused"):
            if product[key] != p[key]:
                raise ValueError("INDEPENDENT_PAUSE_POLICY_MISMATCH")
    elif product["product_id"] == "B25-adjustment":
        if product["basis_date"] != op["as_of"][:10] or product["fq"] != p["fq"]:
            raise ValueError("INDEPENDENT_ADJUSTMENT_BASIS_MISMATCH")
        if (start, end) != ("2024-06-12", "2024-06-13"):
            raise ValueError("INDEPENDENT_ADJUSTMENT_DATE_MISMATCH")
    else:
        if (product["start_date"], product["end_date"]) != (start, end) or p["fq"] is not None:
            raise ValueError("INDEPENDENT_DATE_OR_FQ_MISMATCH")
    for row in product["rows"]:
        if not start <= daily_date(row["date"]) <= end:
            raise ValueError("INDEPENDENT_ROW_OUTSIDE_REQUEST")


def owner_snapshot(product, op):
    validate_scope(product, op)
    fields = op["parameters"]["fields"]
    rows, capabilities = [], []
    for row in product["rows"]:
        output = {"date": daily_date(row["date"])}
        for field in fields:
            cell = row["fields"].get(
                field, dict(state="unsupported", value=None, reason="owner field absent")
            )
            capabilities.append(dict(date=output["date"], field=field, **cell))
            if cell["state"] == "value":
                output[field] = cell["value"]
            elif cell["state"] == "nan":
                output[field] = {"nonfinite": "nan"}
            elif cell["state"] != "unsupported":
                raise ValueError("INDEPENDENT_FIELD_STATE_UNKNOWN")
        rows.append(output)
    unavailable = capabilities and all(c["state"] == "unsupported" for c in capabilities)
    source = product["source"]
    provenance = dict(
        namespace="ashare-data-independent",
        dataset_id=product["product_id"] + ":" + source["sha256"],
        manifest_sha256=product["binding_sha256"],
        evidence_kind="owner_data",
        source_refs=[source["url"], "sha256:" + source["sha256"]],
        quality_conclusion="retrospective independent source product; source precision and owner model retained; not PIT",
    )
    return Snapshot(
        origin=provenance,
        request=request(op),
        status="unsupported" if unavailable else "available",
        rows=[] if unavailable else rows,
        reason="; ".join(sorted({c["reason"] for c in capabilities})) if unavailable else None,
        units=product.get("units", {}),
        captured_at=source.get("captured_end"),
        published_at=source.get("source_publication_time"),
        revision_id=source.get("source_revision"),
    ), capabilities


def reference_snapshot(value, op, case):
    rows = []
    for row in value.get("rows", []):
        converted = {"date": daily_date(row["index"])}
        for field in op["parameters"]["fields"]:
            if field in row:
                converted[field] = row[field]
        rows.append(converted)
    return Snapshot(
        origin=dict(
            namespace="joinquant-frozen-reference",
            dataset_id=case["case_id"],
            manifest_sha256=case["files"]["events.jsonl"],
            evidence_kind="jq_observation",
            source_refs=[
                case["reference_binding"]["run_url"],
                "sha256:" + case["files"]["events.jsonl"],
            ],
            quality_conclusion="frozen original observation; no new JQ run; historical visibility not certified",
        ),
        request=request(op),
        status="error" if "error_type" in value else "available",
        rows=rows,
        reason=json.dumps(
            {k: v for k, v in value.items() if k.startswith("error")}, ensure_ascii=False
        )
        if "error_type" in value
        else None,
        units=dict(volume="share", money="CNY"),
    )


def compare_product(cid, product, op, reference, case):
    owner, capabilities = owner_snapshot(product, op)
    jq = reference_snapshot(reference, op, case)
    mode = {"B27": "volume_money", "C02": "field_coverage", "C03": "history"}.get(cid, "rows")
    result = compare(jq, owner, mode=mode, keys=["date"], fields=op["parameters"]["fields"])
    # Exact typed comparison is retained. A separate label explains 0/1 vs boolean;
    # NaN vs True remains a material missingness difference.
    for diff in result.get("comparison", {}).get("differences", []):
        if (
            diff["field"] == "paused"
            and type(diff["right"]) is bool
            and number(diff["left"]) in (Decimal(0), Decimal(1))
        ):
            diff["boolean_meaning_equal"] = bool(number(diff["left"])) == diff["right"]
    result.update(
        request_ref_seq=op["request_ref_seq"],
        label=op["label"],
        owner_field_capabilities=capabilities,
        owner_product_metadata={k: v for k, v in product.items() if k != "rows"},
        query_scope_mapping="comparison target only; AD API does not implement JQ call or historical as_of visibility",
        jq_response_metadata={k: v for k, v in reference.items() if k != "rows"},
    )
    return result


def collect(owner_python, binding, wheel):
    env = {"PATH": os.defpath, "TZ": "UTC", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    result = subprocess.run(
        [
            str(owner_python),
            "-I",
            "-B",
            str(Path(__file__).with_name("independent_worker.py")),
            str(binding),
            str(wheel),
        ],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("INDEPENDENT_PUBLIC_READ_FAILED:" + result.stderr[-4000:])
    return json.loads(result.stdout)


def run(owner_python, binding, wheel, reference_binding, output):
    output.mkdir(exist_ok=False, parents=True)
    public = collect(owner_python, binding, wheel)
    dump(output / "public-products.json", public)
    # JQ is first read after all independent owner results are computed and saved.
    bindings = json.loads(reference_binding.read_text())
    inputs_path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(inputs_path.read_text())["cases"]
    reports = {}
    for cid in CASES:
        case = next(c for c in bindings["cases"] if c["case_id"] == cid)
        for name, digest in case["files"].items():
            if (
                sha(Path(case["folder"]) / name) != digest
                or inputs[cid]["reference_files"][name] != digest
            ):
                raise ValueError("INDEPENDENT_REFERENCE_CHANGED")
        events = [
            json.loads(line, parse_float=Decimal)
            for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
        ]
        comparisons, gaps = [], []
        for op in inputs[cid]["operations"]:
            names = selection(cid, op)
            if not names:
                gaps.append(
                    dict(
                        operation=op,
                        reason="independent batch1 product unavailable; no substituted source",
                    )
                )
                continue
            for name in names:
                r = compare_product(
                    cid, public["products"][name], op, expected(events, op["request_ref_seq"]), case
                )
                r["owner_read_name"] = name
                comparisons.append(r)
        reports[cid] = dict(
            schema="aq.independent-case-comparison.v1",
            case_id=cid,
            status="INDEPENDENT_RETROSPECTIVE_QUERY_PORTIONS_COMPARED",
            whole_case_pass=False,
            comparisons=comparisons,
            missing_operations=gaps,
            exact_difference_count=sum(
                len(c.get("comparison", {}).get("differences", [])) for c in comparisons
            ),
            capability_gap_count=sum(c["status"] == "CAPABILITY_GAP_RECORDED" for c in comparisons),
            new_JQ_runs=0,
            new_native_runs=0,
        )
        dump(output / f"{cid}.json", reports[cid])
    summary = dict(
        schema="aq.independent-batch.v1",
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        public_read={k: v for k, v in public.items() if k != "products"},
        cases={
            cid: {k: v for k, v in r.items() if k not in ("comparisons", "missing_operations")}
            for cid, r in reports.items()
        },
    )
    summary["artifacts"] = {p.name: sha(p) for p in sorted(output.glob("*.json"))}
    summary["input_bindings_sha256"] = sha(reference_binding)
    summary["query_inputs_sha256"] = sha(inputs_path)
    summary["calculator_sources"] = {
        n: sha(Path(__file__).with_name(n))
        for n in (
            "independent_worker.py",
            "independent_queries.py",
            "query_math.py",
            "query_compare.py",
        )
    }
    summary["B12_opening"] = (
        "read and sealed as market fact only; no auction matching/account verdict change"
    )
    dump(output / "complete.json", summary)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("owner-python", "binding", "wheel", "reference-binding", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    value = run(args.owner_python, args.binding, args.wheel, args.reference_binding, args.output)
    print(json.dumps(value["cases"], ensure_ascii=False))


if __name__ == "__main__":
    main()
