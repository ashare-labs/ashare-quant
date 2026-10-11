"""Consume explicit owner standardization and single-capture evidence for audit only."""

import argparse
import json
import os
import subprocess
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .independent_queries import dump, reference_snapshot, request, sha
from .query_compare import expected
from .query_math import Request, compare
from .query_projection import MODEL, project, sealed_products
from .repair_worker import PIN, WHEEL

STANDARDIZATION = "raw_identity_previous_session_close.v1"
COUNT_MODEL = "aq.observed-row-count-comparison.v1"


def standardized_projection(product, op, *, standardization):
    if (
        standardization != STANDARDIZATION
        or product.get("standardization_policy") != standardization
    ):
        raise ValueError("REPAIR_EXPLICIT_STANDARDIZATION_REQUIRED")
    if product["product_id"] != "C02-standardized":
        raise ValueError("REPAIR_STANDARDIZED_PRODUCT_REQUIRED")
    # The owner supplies these derived values; AQ does not recompute them.
    for row in product["rows"]:
        previous = row["fields"]["pre_close"]
        factor = row["fields"]["factor"]
        if (
            previous.get("origin") != "derived_previous_session_raw_close"
            or previous.get("exchange_ex_reference") is not False
            or previous.get("state") != "value"
            or not previous.get("previous_date")
            or date.fromisoformat(previous["previous_date"]) >= date.fromisoformat(row["date"])
            or factor.get("origin") != "declared_raw_price_identity"
            or factor.get("corporate_action_factor") is not False
            or factor.get("state") != "value"
            or factor.get("value") != "1"
        ):
            raise ValueError("REPAIR_STANDARDIZATION_MEANING_CHANGED")
    return project(product, op, model=MODEL)


def count_projection(product, op, *, model):
    if model != COUNT_MODEL:
        raise ValueError("REPAIR_OBSERVED_COUNT_MODEL_REQUIRED")
    p = op["parameters"]
    dates = [r["date"] for r in product["rows"]]
    count = p.get("count")
    if (
        product["product_id"] != "C05-single-capture"
        or type(count) is not int
        or count <= 0
        or len(dates) != count
        or dates != sorted(set(dates))
        or dates[0] != product["start_date"]
        or dates[-1] != product["end_date"]
        or p.get("start_date") is not None
        or p.get("fill_paused") is not False
        or p.get("skip_paused") is not False
    ):
        raise ValueError("REPAIR_OBSERVED_COUNT_SCOPE_MISMATCH")
    bounded = deepcopy(op)
    del bounded["parameters"]["count"]
    bounded["parameters"]["start_date"] = product["start_date"]
    projected, provenance = project(product, bounded, model=MODEL)
    # Restore the full original count request in the comparison. The range above
    # validates the supplied response, not calendar completeness or historical access.
    projected = projected.model_copy(update={"request": Request.model_validate(request(op))})
    provenance.update(
        count_model=model,
        observed_rows=count,
        full_session_coverage_certified=False,
        actual_capture_versions=1,
        meaning="compare supplied observed rows to the original count request; not certified last trading sessions",
    )
    return projected, provenance


def halt_attribution(history, halts, previous_history, missing_dates):
    if (
        history["security"] != halts["security"]
        or history["security"] != previous_history["security"]
        or history["rows"] != previous_history["rows"]
    ):
        raise ValueError("REPAIR_HISTORY_CHANGED")
    if any(
        v.get("role") != "independent_retrospective_query_research"
        or any(
            v.get(k) is not False for k in ("historical_pit", "strategy_visible", "matching_feed")
        )
        for v in (history, halts)
    ):
        raise ValueError("REPAIR_HALT_CAPABILITY_CHANGED")
    days = [r["date"] for r in halts["rows"]]
    if len(days) != len(set(days)) or set(days) != set(missing_dates):
        raise ValueError("REPAIR_HALT_DATES_MISMATCH")
    if set(days) & {r["date"] for r in history["rows"]}:
        raise ValueError("REPAIR_HALT_CONFLICTS_WITH_HISTORY")
    if any(
        r.get("full_day_paused") is not True
        or not r.get("source_content")
        or r.get("official_announcement_chain_verified") is not False
        for r in halts["rows"]
    ):
        raise ValueError("REPAIR_FULL_DAY_SOURCE_EVENT_REQUIRED")
    return dict(
        status="OWNER_DOCUMENTED_FULL_DAY_HALTS_RECEIVED",
        daily_rows_unchanged=True,
        daily_rows=len(history["rows"]),
        documented_dates=sorted(days),
        unexplained_in_previous_missing_set=[],
        classification_origin="AD public event records",
        official_notice_chain_certified=False,
        historical_pit_certified=False,
        inserted_bars=0,
        absolute_earliest_certified=False,
        source=halts["source"],
        evidence_rows=halts["rows"],
    )


def capture_window(events, request_seq):
    starts = [e for e in events if e["kind"] == "capture_start" and e["seq"] < request_seq]
    ends = [e for e in events if e["kind"] == "capture_end" and e["seq"] > request_seq]
    if not starts or not ends:
        return None
    start = max(starts, key=lambda e: e["seq"])
    end = min(ends, key=lambda e: e["seq"])
    clocks = [datetime.fromisoformat(e["observed_wall_utc"]) for e in (start, end)]
    if (
        any(t.tzinfo is None or t.utcoffset() is None for t in clocks)
        or clocks[0] > clocks[1]
        or start.get("run_tag") != end.get("run_tag")
    ):
        raise ValueError("REPAIR_CAPTURE_WINDOW_INVALID")
    return dict(
        start=clocks[0].isoformat(),
        end=clocks[1].isoformat(),
        start_seq=start["seq"],
        end_seq=end["seq"],
        meaning="original observed capture envelope; not source publication",
    )


def collect(owner_python, binding, wheel):
    result = subprocess.run(
        [
            str(owner_python),
            "-I",
            "-B",
            str(Path(__file__).with_name("repair_worker.py")),
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
        raise ValueError("REPAIR_PUBLIC_READ_FAILED:" + result.stderr[-4000:])
    return json.loads(result.stdout)


def run(owner_python, binding, wheel, reference_binding, prior_run, output):
    output.mkdir(parents=True, exist_ok=False)
    public = collect(owner_python, binding, wheel)
    dump(output / "public-products.json", public)
    products = public["products"]
    prior = sealed_products(prior_run)
    # Reuse the owner's previously sealed missing-date report, not JQ responses.
    old_coverage = binding.parent.parent / "independent-query-delivery/reports/c03-coverage.json"
    if sha(old_coverage) != "077708145e7698cef5924513f3a7d47d8d75af5aaac7f33b0e900629dda10ad5":
        raise ValueError("REPAIR_PREVIOUS_COVERAGE_CHANGED")
    missing = json.loads(old_coverage.read_text())["calendar_comparison"]["missing_dates"]
    attribution = halt_attribution(
        products["c03-history"],
        products["c03-halt-events"],
        prior["C03-history-sina-pingan"],
        missing,
    )
    dump(output / "C03-halt-attribution.json", attribution)
    inputs_path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(inputs_path.read_text())["cases"]
    bindings = json.loads(reference_binding.read_text())
    reports = {}
    for cid in ("C02", "C03", "C05"):
        case = next(c for c in bindings["cases"] if c["case_id"] == cid)
        for name, digest in case["files"].items():
            if (
                sha(Path(case["folder"]) / name) != digest
                or inputs[cid]["reference_files"][name] != digest
            ):
                raise ValueError("REPAIR_REFERENCE_CHANGED")
        projections, gaps = [], []
        for op in inputs[cid]["operations"]:
            if op["api"] != "get_price":
                gaps.append(
                    dict(
                        operation=op,
                        reason="No new independent public product for this original operation",
                    )
                )
                continue
            names = (
                ["c02-sina-pingan", "c02-eastmoney-pingan"]
                if cid == "C02"
                else ["c03-history"]
                if cid == "C03"
                else ["c05-single-capture"]
            )
            for name in names:
                product = products[name]
                if cid == "C02":
                    value, provenance = standardized_projection(
                        product, op, standardization=STANDARDIZATION
                    )
                elif cid == "C05":
                    value, provenance = count_projection(product, op, model=COUNT_MODEL)
                else:
                    value, provenance = project(product, op, model=MODEL)
                projections.append((op, name, value, provenance))
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
        # All owner reads and projections above are saved before JQ outputs open.
        events = [
            json.loads(line, parse_float=Decimal)
            for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
        ]
        comparisons = []
        for op, name, value, provenance in projections:
            jq = reference_snapshot(expected(events, op["request_ref_seq"]), op, case)
            window = capture_window(events, op["request_ref_seq"]) if cid == "C05" else None
            if window:
                jq = type(jq).model_validate(
                    {**jq.model_dump(mode="json"), "captured_at": window["end"]}
                )
            result = compare(
                jq,
                value,
                mode={"C02": "field_coverage", "C03": "history", "C05": "freshness"}[cid],
                keys=["date"],
                fields=op["parameters"]["fields"],
            )
            result.update(
                owner_product=name,
                owner_metadata={k: v for k, v in products[name].items() if k != "rows"},
                owner_field_capabilities=provenance["owner_field_capabilities"],
                projection_model=provenance["model"],
                request_ref_seq=op["request_ref_seq"],
            )
            if cid == "C05":
                result["reference_capture_window"] = window
            comparisons.append(result)
        reports[cid] = dict(
            case_id=cid,
            comparisons=comparisons,
            missing_operations=gaps,
            exact_differences=sum(
                len(c.get("comparison", {}).get("differences", [])) for c in comparisons
            ),
            capability_gaps=sum(c["status"] == "CAPABILITY_GAP_RECORDED" for c in comparisons),
            whole_case_pass=False,
            new_JQ_runs=0,
            new_native_runs=0,
        )
        dump(output / f"{cid}.json", reports[cid])
    summary = dict(
        schema="aq.independent-query-repair-batch.v1",
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        public_read={k: v for k, v in public.items() if k != "products"},
        cases={
            cid: {k: v for k, v in r.items() if k not in ("comparisons", "missing_operations")}
            for cid, r in reports.items()
        },
        artifacts={p.name: sha(p) for p in sorted(output.glob("*.json"))},
        input_bindings_sha256=sha(reference_binding),
        query_inputs_sha256=sha(inputs_path),
        calculator_sources={
            n: sha(Path(__file__).with_name(n))
            for n in (
                "repair_worker.py",
                "independent_repair.py",
                "query_projection.py",
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
    for name in ("owner-python", "binding", "wheel", "reference-binding", "prior-run", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.owner_python,
                args.binding,
                args.wheel,
                args.reference_binding,
                args.prior_run,
                args.output,
            )["cases"],
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
