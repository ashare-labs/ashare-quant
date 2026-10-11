"""Execute frozen query inputs without loading any reference response or account."""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_quant.diagnostics.runner import digest
from ashare_quant.identity import source_inventory

from .queries import QueryFacade, QueryRefused


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(case_id, binding, owner_python, output):
    path = Path(__file__).with_name("query-inputs.json")
    inputs = json.loads(path.read_text())["cases"][case_id]
    root = Path(output).resolve()
    # Execution attempts never overwrite or silently resume a partial artifact.
    root.mkdir(parents=True, exist_ok=False)
    before = source_inventory()
    facade = QueryFacade(binding, owner_python, inputs["group"])
    manifest = dict(
        schema="aq.round-one.query-run.v1",
        case_id=case_id,
        backend_sources=before,
        input_sha256=file_sha(path),
        input=inputs,
        provider=facade.descriptor(),
        native_run=False,
        scope="query_portion_only_no_native_lifecycle_or_account_execution",
    )
    manifest["identity"] = digest(manifest)
    save(root / "manifest.json", manifest)
    events = []
    for op in inputs["operations"]:
        event = dict(seq=len(events) + 1, kind="query_request", **op)
        events.append(event)
        try:
            value = facade.query(**{k: op[k] for k in ("api", "parameters", "as_of", "role")})
            events.append(
                dict(
                    seq=len(events) + 1,
                    kind="query_return",
                    request_seq=event["seq"],
                    label=op["label"],
                    as_of=op["as_of"],
                    value=value,
                )
            )
        except QueryRefused as exc:
            events.append(
                dict(
                    seq=len(events) + 1,
                    kind="query_exception",
                    request_seq=event["seq"],
                    label=op["label"],
                    as_of=op["as_of"],
                    error=exc.to_dict(),
                )
            )
        # Catching a query rejection keeps the query session usable. These are
        # actual API calls; this driver makes no claim to execute native callbacks.
        with (root / "events.jsonl").open("w") as stream:
            for item in events:
                stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
    if source_inventory() != before:
        raise RuntimeError("QUERY_BACKEND_CHANGED_DURING_RUN")
    result = dict(
        case_id=case_id,
        identity=manifest["identity"],
        status="QUERY_INPUTS_EXECUTED_COMPARISON_PENDING",
        queries=len(inputs["operations"]),
        returned=sum(e["kind"] == "query_return" for e in events),
        refused=sum(e["kind"] == "query_exception" for e in events),
        dataset_id=facade.dataset_id,
        native_run=False,
        real_source_observations=True,
        historical_pit=False,
        new_jq_run=False,
    )
    save(root / "result.json", result)
    save(
        root / "complete.json",
        dict(
            identity=manifest["identity"],
            artifacts={
                name: file_sha(root / name)
                for name in ("manifest.json", "events.jsonl", "result.json")
            },
        ),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--binding", required=True)
    parser.add_argument("--owner-python", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.case_id, args.binding, args.owner_python, args.output)))


if __name__ == "__main__":
    main()
