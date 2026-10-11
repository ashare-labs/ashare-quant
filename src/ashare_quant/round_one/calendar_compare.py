"""B21 native calendar/callback comparison, separate from prior query-only evidence."""

from pathlib import Path

from .compare import clock, compare_d1, fields, read_json, read_json_line
from .query_compare import compare as compare_queries


def compare(binding, aq_root):
    if binding["case_id"] != "B21":
        raise ValueError("CALENDAR_COMPARISON_SCOPE")
    report = compare_d1(binding, aq_root)
    jq = [
        read_json_line(x)
        for x in (Path(binding["folder"]) / "events.jsonl").read_text().splitlines()
    ]
    aq = [read_json_line(x) for x in (Path(aq_root) / "events.jsonl").read_text().splitlines()]
    for kind in ("day_start", "calendar_probe"):
        lefts, rights = ([e for e in events if e["kind"] == kind] for events in (jq, aq))
        report["comparisons"].extend(
            fields({"count": len(lefts)}, {"count": len(rights)}, {"label": kind + "_sequence"})
        )
        for left, right in zip(lefts, rights):
            report["comparisons"].extend(
                fields(
                    {"at": clock(left["time"]), "previous_date": left["previous_date"]},
                    {"at": clock(right["at"]), "previous_date": right["previous_date"]},
                    dict(label=kind, jq_event_seq=left["seq"], aq_event_seq=right["seq"]),
                )
            )
    report["query_comparison"] = compare_queries(binding, aq_root)
    report["differences"] = [
        r
        for r in report["comparisons"]
        if r["status"] not in {"MATCH", "MATCH_NUMERIC_DIFFERENT_TEXT"}
    ]
    report["unresolved_count"] = (
        sum(
            r.get("classification") in {None, "REQUIRES_INVESTIGATION"}
            for r in report["differences"]
        )
        + report["query_comparison"]["unresolved_count"]
    )
    report["status"] = (
        "DIFFERENCES_ATTRIBUTED"
        if not report["unresolved_count"]
        else "DIFFERENCES_NEED_INVESTIGATION"
    )
    result = read_json(Path(aq_root) / "result.json")
    report.update(
        execution_sessions=result["execution_sessions"],
        calendar_adjacency=result["calendar_adjacency"],
        data_evidence_class="independent_SZSE_calendar_for_native_events_and_JQ_observed_queries_for_strategy_query_role",
        actual_empty_account_calendar_executed=True,
        holdings_overnight_admitted=False,
    )
    return report
