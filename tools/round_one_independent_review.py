"""Review a sealed batch1 comparison; no market acquisition or new platform runs."""

import hashlib
import json
from collections import Counter
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ref(path):
    return dict(path=str(path.resolve()), sha256=sha(path))


def read(path):
    return json.loads(path.read_text())


def review(root):
    run = root / ".verification/round-one/independent-batch1-dev10/source-run"
    installed = root / ".verification/round-one/independent-batch1-dev10/installed-run"
    seal = read(run / "complete.json")
    for name, digest in seal["artifacts"].items():
        assert sha(run / name) == digest, name
        assert (run / name).read_bytes() == (installed / name).read_bytes(), name
    assert (run / "complete.json").read_bytes() == (installed / "complete.json").read_bytes()
    public = read(run / "public-products.json")
    # Host-local location is explicit; no directory search or alternate inputs.
    owner = Path("/Users/yinming/Documents/Codex/2026-10-10/task-3/independent-query-delivery")
    handoff = read(owner / "handoff-batch1.json")
    assert sha(owner / "binding.json") == seal["owner_binding_sha256"]
    for name, value in public["products"].items():
        raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        assert hashlib.sha256(raw).hexdigest() == handoff["products"][name]["sha256"], name
    cases = {}
    for cid in ("B27", "C02", "B25", "B28", "C03"):
        report = read(run / f"{cid}.json")
        cases[cid] = dict(report=ref(run / f"{cid}.json"), comparisons=[])
        for c in report["comparisons"]:
            delta = c.get("comparison", {})
            caps = c["owner_field_capabilities"]
            cases[cid]["comparisons"].append(
                dict(
                    label=c["label"],
                    owner_product=c["owner_read_name"],
                    as_of=c["request"]["as_of"],
                    fields=c["request"]["parameters"]["fields"],
                    status=c["status"],
                    shared_rows=delta.get("shared_keys"),
                    jq_only_rows=len(delta.get("left_only", [])),
                    ad_only_rows=len(delta.get("right_only", [])),
                    exact_field_differences=delta.get("differences", []),
                    unsupported=sorted({x["field"] for x in caps if x["state"] == "unsupported"}),
                    owner_field_origins=dict(Counter(x.get("origin", "unsupported") for x in caps)),
                    derived=c.get("computed"),
                )
            )
        cases[cid]["missing_operations"] = report["missing_operations"]
    binding = read(root / "docs/round-one/input-bindings.json")
    case = next(c for c in binding["cases"] if c["case_id"] == "B12")
    events_path = Path(case["folder"]) / "events.jsonl"
    assert sha(events_path) == case["files"]["events.jsonl"]
    events = [json.loads(x, parse_float=str) for x in events_path.read_text().splitlines()]
    jq = next(e for e in events if e["kind"] == "call_auction_data")
    point = public["products"]["B12-opening"]["rows"][0]
    from decimal import Decimal

    opening = dict(
        jq_event_seq=jq["seq"],
        jq_observed_at=jq["time"],
        jq_label=jq["rows"][0]["time"],
        AD_row=point,
        jq_row=jq["rows"][0],
        price_delta=str(Decimal(point["price"]) - Decimal(jq["rows"][0]["current"])),
        volume_shares_delta=str(Decimal(point["volume_shares"]) - Decimal(jq["rows"][0]["volume"])),
        independent_order_auction_matching_certified=False,
        limitation="same aggregate price/volume does not certify participation, matching order or fill timing",
    )
    return dict(
        schema="aq.independent-query-review.v1",
        run=ref(run / "complete.json"),
        source_installed_reports_identical=True,
        owner_handoff=ref(owner / "handoff-batch1.json"),
        owner_binding=ref(owner / "binding.json"),
        owner_installed_files_checked=public["owner_installed_files_checked"],
        public_products_verified=17,
        comparison_count=sum(len(c["comparisons"]) for c in cases.values()),
        cases=cases,
        B12_market_fact_only=opening,
        new_JQ_runs=0,
        new_native_runs=0,
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    result = review(root)
    p = root / "docs/round-one/independent-query-review-dev10.json"
    with p.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            dict(
                public_products=result["public_products_verified"],
                comparisons=result["comparison_count"],
            )
        )
    )
