"""Recheck frozen references, sealed outputs, comparator results and execution sources."""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from ashare_quant.round_one.carry_compare import IDS as CARRY_IDS
from ashare_quant.round_one.carry_compare import compare as compare_carry
from ashare_quant.round_one.compare import compare_d1, read_json_line
from ashare_quant.round_one.dividend_compare import compare as compare_dividend
from ashare_quant.round_one.market_compare import compare_markets
from ashare_quant.round_one.query_compare import compare as compare_queries

EXECUTIONS = {
    **dict.fromkeys(("B08", "B09", "B10", "B11", "B12", "B13"), "ca3f9a0"),
    **dict.fromkeys(("B15", "B19"), "24aba588"),
    **dict.fromkeys(("B29", "B30", "B32"), "8bc0f54"),
    **dict.fromkeys(("B36", "B37", "B38", "B39"), "cf6e10a"),
    **dict.fromkeys(("B24", "B31"), "347cf5a"),
    "B20": "ce5a1a5",
    "B35": "d2776e7",
    **dict.fromkeys(("B14", "B16", "B17", "B18"), "efb954a"),
    **dict.fromkeys(("B22", "B33"), "7482612"),
    "B26": "d1b8968",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def audit(root):
    docs = root / "docs/round-one"
    bindings = load(docs / "input-bindings.json")
    assert (
        sha(Path(bindings["root"]) / "docs/round-one-manifest.json") == bindings["manifest_sha256"]
    )
    by_id = {c["case_id"]: c for c in bindings["cases"]}
    references = {}
    for case in by_id.values():
        for name, expected in case["files"].items():
            p = Path(case["folder"]) / name
            assert sha(p) == expected, str(p)
            references[str(p)] = expected
    archives = {}

    def source_check(run_root, revision):
        commit = subprocess.check_output(
            ["git", "rev-parse", revision], cwd=root, text=True
        ).strip()
        if commit not in archives:
            data = subprocess.check_output(["git", "archive", commit, "src"], cwd=root)
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                archives[commit] = {
                    m.name.removeprefix("src/"): hashlib.sha256(
                        tar.extractfile(m).read()
                    ).hexdigest()
                    for m in tar
                    if m.isfile()
                }
        manifest = load(run_root / "manifest.json")
        inventory = manifest["backend_sources"]
        for name, expected in inventory.items():
            assert archives[commit].get(name) == expected, (commit, name)
        return commit, len(inventory)

    native = []
    for group in (
        "d1-results",
        "d2-single-results",
        "d5-single-results",
        "d6-single-results",
        "combined-results",
        "next-batch-results",
        "carry-results",
        "dividend-results",
    ):
        for path in sorted((docs / group).glob("B*.json")):
            old = load(path)
            cid = old["case_id"]
            run_root = Path(old["aq_run_root"])
            comparator = (
                compare_dividend
                if cid == "B26"
                else (compare_carry if cid in CARRY_IDS else compare_d1)
            )
            new = comparator(by_id[cid], run_root)
            if cid in CARRY_IDS or cid == "B26":
                # Include the unchanged owner facts, explicit assumptions and
                # unknowns, not only the compared numerical fields.
                assert new == old, (cid, "complete_carry_report")
            for key in (
                "differences",
                "status",
                "unresolved_count",
                "aq_complete_sha256",
            ):
                assert new[key] == old[key], (cid, key)

            def canonical(row):
                row = dict(row)
                if row.get("label") == "order_intent":
                    row["label"] = "order_request"
                return row

            current_rows = [canonical(r) for r in new["comparisons"]]
            for row in old["comparisons"]:
                normalized = canonical(row)
                assert normalized in current_rows, (cid, normalized)
                current_rows.remove(normalized)
            assert all(r["status"] == "MATCH" and r["field"] == "/count" for r in current_rows)
            nested = {}
            if "native_queries" in old:
                nested["native_queries"] = compare_queries(by_id[cid], run_root)
                assert nested["native_queries"] == old["native_queries"], (cid, "queries")
            if "market_queries" in old:
                jq = [
                    read_json_line(line)
                    for line in (Path(by_id[cid]["folder"]) / "events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                aq = [
                    read_json_line(line)
                    for line in (run_root / "events.jsonl").read_text().splitlines()
                ]
                nested["market_queries"] = compare_markets(jq, aq)
                if cid in CARRY_IDS and nested["market_queries"]["fields"] == 0:
                    nested["market_queries"]["status"] = "NO_SEPARATE_MARKET_OBSERVATION_EVENTS"
                assert nested["market_queries"] == old["market_queries"], (cid, "markets")
            commit, count = source_check(run_root, EXECUTIONS[cid])
            native.append(
                dict(
                    case_id=cid,
                    run_root=str(run_root),
                    execution_commit=commit,
                    execution_source_files_checked=count,
                    complete_sha256=sha(run_root / "complete.json"),
                    report=str(path),
                    report_sha256=sha(path),
                    field_count=len(new["comparisons"]),
                    historical_report_field_count=len(old["comparisons"]),
                    additional_current_sequence_checks=current_rows,
                    unresolved=new["unresolved_count"],
                    nested_rechecked=list(nested),
                )
            )
    from ashare_quant.round_one.dividend_tax_compare import compare as compare_net

    net_path = docs / "dividend-net-results/B26.json"
    net_old = load(net_path)
    net_root = Path(net_old["aq_run_root"])
    assert compare_net(by_id["B26"], net_root) == net_old
    net_commit, net_count = source_check(net_root, "87c45ab")
    compatibility = dict(
        case_id="B26",
        model="aq.b26-jq-fixed-dividend-tax.v1",
        run_root=str(net_root),
        execution_commit=net_commit,
        execution_source_files_checked=net_count,
        complete_sha256=sha(net_root / "complete.json"),
        report=str(net_path),
        report_sha256=sha(net_path),
        field_count=len(net_old["comparisons"]),
        difference_count=len(net_old["differences"]),
        gross_baseline_preserved=True,
    )
    from ashare_quant.round_one.calendar_compare import compare as compare_calendar

    calendar_runs = []
    for path in sorted((docs / "calendar-results").glob("B*.json")):
        old = load(path)
        cid = old["case_id"]
        run_root = Path(old["aq_run_root"])
        comparator = compare_calendar if cid == "B21" else compare_d1
        assert comparator(by_id[cid], run_root) == old, cid
        commit, count = source_check(run_root, "290083b" if cid == "B21" else "bca6870")
        calendar_runs.append(
            dict(
                case_id=cid,
                run_root=str(run_root),
                execution_commit=commit,
                execution_source_files_checked=count,
                complete_sha256=sha(run_root / "complete.json"),
                report=str(path),
                report_sha256=sha(path),
            )
        )
    assert len(calendar_runs) == 6
    queries = []
    for path in sorted((docs / "query-results").glob("[BC]*.json")):
        old = load(path)
        cid = old["case_id"]
        run_root = Path(old["aq_root"])
        assert compare_queries(by_id[cid], run_root) == old, cid
        commit, count = source_check(run_root, "e865c4e")
        queries.append(
            dict(
                case_id=cid,
                run_root=str(run_root),
                execution_commit=commit,
                execution_source_files_checked=count,
                complete_sha256=sha(run_root / "complete.json"),
                report=str(path),
                report_sha256=sha(path),
                fields=old["fields"],
            )
        )
    e03 = load(docs / "e03-result.json")
    for name, expected in e03["artifacts"].items():
        assert sha(Path(e03["root"]) / name) == expected, name
    from round_one_e03_audit import audit as audit_recovery

    assert audit_recovery(e03["root"]) == e03
    from round_one_query_review import audit as review_queries

    query_review = docs / "query-scope-review-dev5.json"
    assert review_queries(root) == load(query_review)
    from round_one_goal_review import generate as review_goals

    previous_goal_review = review_goals(root, write=False)
    goal_review = review_goals(root, write=False, version="dev8")
    reservation_path = docs / "reservation-results/B10.json"
    reservation_old = load(reservation_path)
    reservation_root = Path(reservation_old["aq_run_root"])
    assert compare_d1(by_id["B10"], reservation_root) == reservation_old
    reservation_commit, reservation_count = source_check(reservation_root, "beb4c67")
    reservation = dict(
        case_id="B10",
        model="aq.zero-cost-partial-limit-reservation.v1",
        run_root=str(reservation_root),
        execution_commit=reservation_commit,
        execution_source_files_checked=reservation_count,
        complete_sha256=sha(reservation_root / "complete.json"),
        report=str(reservation_path),
        report_sha256=sha(reservation_path),
        field_count=len(reservation_old["comparisons"]),
        difference_count=len(reservation_old["differences"]),
        default_baseline_preserved=True,
    )
    assert len(native) == len(EXECUTIONS) == 26 and len(queries) == 13
    return dict(
        schema="aq.round-one.evidence-audit.v1",
        status="VERIFIED",
        frozen_reference_file_count=len(references),
        reference_files=references,
        input_bindings_sha256=sha(docs / "input-bindings.json"),
        manifest_sha256=bindings["manifest_sha256"],
        native=native,
        additional_compatibility_runs=[compatibility, reservation],
        additional_calendar_runs=calendar_runs,
        primary_goal_counts=goal_review["counts"],
        primary_goal_review_sha256=sha(docs / "goal-review-dev8.json"),
        preserved_dev7_goal_counts=previous_goal_review["counts"],
        preserved_dev7_goal_review_sha256=sha(docs / "goal-review-dev7.json"),
        query_portions=queries,
        e03_report_sha256=sha(docs / "e03-result.json"),
        e03_rechecked=True,
        query_scope_review_rechecked=True,
        query_scope_review_sha256=sha(query_review),
        execution_source_commits=sorted(archives),
        new_native_runs=0,
        new_jq_runs=0,
    )


if __name__ == "__main__":
    print(json.dumps(audit(Path(sys.argv[1]).resolve()), indent=2, ensure_ascii=False))
