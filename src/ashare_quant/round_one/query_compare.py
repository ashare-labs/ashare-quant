"""Compare sealed query calls to original JQ outputs, only after AQ execution."""

import json
from decimal import Decimal
from pathlib import Path

from ashare_quant.diagnostics.runner import digest

from .compare import fields, read_json, sha

REQUEST_KINDS = {"data_request", "coverage_request", "field_request", "query_request"}
ROW_KINDS = {
    "data_result",
    "query_return",
    "field_return",
    "depth_rows",
    "freshness_rows",
    "universe_rows",
}
ERROR_CODES = {"FutureDataError": "JQ_FUTURE_QUERY", "AssertionError": "JQ_QUERY_FIELD"}


def normalize(value):
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if type(value) in (int, Decimal):
        return format(Decimal(value), "f")
    return value


def expected(events, seq):
    request = events[seq - 1]
    if request["kind"] in {"calendar_probe", "trading_calendar"}:
        return dict(rows=[{"date": d} for d in request.get("trade_days", request.get("dates"))])
    if request["kind"] == "security_info":
        return dict(rows=[request["data"]])
    rows = []
    metadata = {}
    for event in events[seq:]:
        if event["kind"] in REQUEST_KINDS:
            break
        if event["kind"] in ROW_KINDS:
            rows.extend(event["rows"])
            for key in ("columns", "dtypes"):
                if key in event:
                    metadata[key] = event[key]
        elif event["kind"] == "index_members":
            rows.extend({"code": c} for c in event["codes"])
        elif event["kind"] in {"query_exception", "field_exception", "data_exception"}:
            return dict(error_type=event["error_type"], error=event["error"])
    return normalize(dict(rows=rows, **metadata))


def compare(case_binding, root):
    root = Path(root)
    seal = read_json(root / "complete.json")
    manifest = read_json(root / "manifest.json")
    native = manifest["schema"] == "aq.round-one.native.v1"
    required = {"manifest.json", "events.jsonl", "result.json"}
    if native:
        required |= {
            "account.json",
            "config.json",
            "native-config.json",
            "native.log",
            "orders.json",
            "strategy.py",
            "teardown.json",
            "trades.json",
        }
    if set(seal["artifacts"]) != required:
        raise ValueError("QUERY_COMPARISON_INCOMPLETE_SEAL")
    for name, h in seal["artifacts"].items():
        if sha(root / name) != h:
            raise ValueError("QUERY_COMPARISON_ARTIFACT_CHANGED")
    manifest = read_json(root / "manifest.json")
    if (
        manifest["identity"] != seal["identity"]
        or (manifest["case"]["case_id"] if native else manifest["case_id"])
        != case_binding["case_id"]
    ):
        raise ValueError("QUERY_COMPARISON_IDENTITY_CHANGED")
    folder = Path(case_binding["folder"])
    for name in ("events.jsonl", "jq.py", "reference.json"):
        if sha(folder / name) != case_binding["files"][name]:
            raise ValueError("QUERY_REFERENCE_CHANGED")
    jq = [
        json.loads(line, parse_float=Decimal)
        for line in (folder / "events.jsonl").read_text().splitlines()
    ]
    aq = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    aq = [e for e in aq if e["kind"] in {"query_request", "query_return", "query_exception"}]
    inputs = manifest["query_input"] if native else manifest["input"]
    reports = []
    if len(aq) != 2 * len(inputs["operations"]):
        raise ValueError("QUERY_EXECUTION_EVENT_COUNT_CHANGED")
    for index, op in enumerate(inputs["operations"]):
        request, result = aq[2 * index : 2 * index + 2]
        if any(request[k] != op[k] for k in op) or result["request_seq"] != request["seq"]:
            raise ValueError("QUERY_REQUEST_CHANGED")
        left = expected(jq, op["request_ref_seq"])
        report = dict(
            label=op["label"],
            as_of=op["as_of"],
            request_ref_seq=op["request_ref_seq"],
            request_sha256=digest(op),
            scope="fixed_query_observation_only",
            aq_event_seq=result["seq"],
        )
        if "error_type" in left:
            error = result.get("error", {})
            matched = error.get("origin") == "ashare-data" and error.get("code") == ERROR_CODES.get(
                left["error_type"]
            )
            report.update(
                status="GUARD_CATEGORY_MATCH_TYPES_DIFFER" if matched else "QUERY_ERROR_DIFFERENCE",
                reference_error=left,
                aq_error=error,
                fields=1,
                differences=[] if matched else [dict(jq=left, aq=result)],
                exception_text_equal=False,
                stored_error_replay=False,
            )
        elif result["kind"] != "query_return":
            report.update(
                status="QUERY_UNEXPECTED_REFUSAL", fields=0, differences=[dict(jq=left, aq=result)]
            )
        else:
            public = result["value"]
            right = {k: public["result"].get(k) for k in left}
            # AD adds audit-only provenance flags; they are preserved in the run,
            # not treated as JQ-observed fields or admitted to historical strategies.
            right["rows"] = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"future_end_date_observation", "end_date_is_historical_pit_fact"}
                }
                for row in public["rows"]
            ]
            comparisons = fields(left, right, {})
            diffs = [
                r
                for r in comparisons
                if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
            ]
            report.update(
                status="OBSERVED_QUERY_FIELDS_MATCH" if not diffs else "QUERY_FIELDS_DIFFER",
                fields=len(comparisons),
                differences=diffs,
                row_count=len(right["rows"]),
                normalized_jq_sha256=digest(left),
                normalized_aq_sha256=digest(right),
                dataset_id=public["dataset_id"],
                query_id=public["query_id"],
                adjustment_anchor=public["adjustment_anchor"],
                physical_capture_interval=public["physical_capture_interval"],
                historical_pit=public["historical_pit"],
                matching_eligible=public["matching_eligible"],
            )
        reports.append(report)
    unresolved = sum(len(q["differences"]) for q in reports)
    return dict(
        schema="aq.round-one.query-comparison.v1",
        case_id=case_binding["case_id"],
        status="QUERY_PORTION_COMPARED"
        if not unresolved
        else "QUERY_DIFFERENCES_NEED_INVESTIGATION",
        reference_verdict=case_binding["reference_verdict"],
        queries=reports,
        fields=sum(q["fields"] for q in reports),
        unresolved_count=unresolved,
        aq_root=str(root),
        aq_complete_sha256=sha(root / "complete.json"),
        jq_events_sha256=sha(folder / "events.jsonl"),
        native_run=native,
        whole_case_complete=False,
        historical_visibility_verdict="NOT_CERTIFIED",
        scope="shared_source_query_contract_roundtrip_not_independent_market_validation",
        no_claims=[
            "native callback order or lifecycle",
            "account behavior",
            "historical PIT",
            "full JQ SDK",
        ],
    )
