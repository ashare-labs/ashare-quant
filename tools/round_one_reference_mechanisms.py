"""Read-only extraction from pinned B12/B14/B17 observations; never runs JQ."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

IDS = ("B12", "B14", "B17")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select(value, names):
    return {name: value[name] for name in names if name in value}


def source_facts(source):
    tree = ast.parse(source)
    names = ("CONFIG", "ORDER_FIELDS", "TRADE_FIELDS", "FEE_PROBE_FIELDS")
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in names
    }
    assignments["OrderCost_call_keywords"] = [
        [kw.arg for kw in node.keywords]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OrderCost"
    ]
    # Record the original probe implementation without importing/executing it.
    assignments["fields_function"] = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "fields"
    ]
    return assignments


def extract(case, events, source):
    result = dict(
        case_id=case["case_id"],
        event_count=len(events),
        source_facts=source_facts(source),
        verdict_change=None,
        new_JQ_runs=0,
        new_AQ_runs=0,
    )
    result["order_returns"] = [
        dict(
            select(e, ("seq", "time", "commission_repr")),
            order=select(
                e["order"],
                ("security", "amount", "filled", "status", "price", "commission", "action"),
            ),
        )
        for e in events
        if e["kind"] == "order_return"
    ]
    if case["case_id"] == "B12":
        result["auction_data_events"] = [
            select(e, ("seq", "time", "source", "rows"))
            for e in events
            if e["kind"] == "call_auction_data"
        ]
        result["snapshots"] = [
            dict(
                select(e, ("seq", "time", "label", "open_order_ids")),
                orders=[
                    select(o, ("order_id", "filled", "status", "finish_time", "price"))
                    for o in e["orders"]
                ],
                trades=[select(t, ("trade_id", "time", "amount", "price")) for t in e["trades"]],
            )
            for e in events
            if e["kind"] == "snapshot"
        ]
        filled = [e for e in result["snapshots"] if e["trades"]]
        result["first_snapshot_with_trade"] = filled[0] if filled else None
        result["independent_auction_matching_certified"] = False
    else:
        result["fee_probes"] = [
            select(e, ("seq", "time", "order_id", "fields", "note"))
            for e in events
            if e["kind"] == "native_fee_field_probe"
        ]
        result["field_observation_scope"] = "these_recorded_UserOrder_instances_only"
        result["UserTrade_fee_attributes"] = "NOT_PROBED"
        result["OrderCost_transfer_fee_keyword_rejection"] = "NOT_PROBED"
        result["native_fee_components_reconstructed"] = False
    return result


def audit(bindings_path):
    binding = json.loads(bindings_path.read_text())
    manifest = Path(binding["root"]) / "docs/round-one-manifest.json"
    if sha(manifest) != binding["manifest_sha256"]:
        raise ValueError("REFERENCE_MANIFEST_HASH_MISMATCH")
    cases, checked = {}, {}
    for case in binding["cases"]:
        if case["case_id"] not in IDS:
            continue
        for name, expected in case["files"].items():
            path = Path(case["folder"]) / name
            if sha(path) != expected:
                raise ValueError("REFERENCE_FILE_HASH_MISMATCH:" + str(path))
            checked[str(path)] = expected
        source = (Path(case["folder"]) / "jq.py").read_text()
        # Preserve the original decimal representation instead of float conversion.
        events = [
            json.loads(line, parse_float=str)
            for line in (Path(case["folder"]) / "events.jsonl").read_text().splitlines()
        ]
        cases[case["case_id"]] = extract(case, events, source)
    if set(cases) != set(IDS):
        raise ValueError("REFERENCE_CASE_MISSING")
    return dict(
        schema="aq.reference-mechanism-audit.v1",
        status="OBSERVATIONS_EXTRACTED",
        input_bindings_sha256=sha(bindings_path),
        source_manifest_sha256=sha(manifest),
        original_files_checked=checked,
        cases=cases,
        whole_case_verdicts_changed=0,
        new_JQ_runs=0,
        new_AQ_runs=0,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.bindings)
    result["extractor_sha256"] = sha(Path(__file__))
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(dict(status=result["status"], cases=list(result["cases"]))))


if __name__ == "__main__":
    main()
