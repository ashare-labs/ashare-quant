"""Verify sealed C04 comparisons and receive pinned owner gap reports, read-only."""

import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def ref(path):
    return dict(path=str(path.resolve()), sha256=sha(path))


def review(root):
    run = root / ".verification/round-one/independent-index-dev12/source-run"
    installed = run.parent / "installed-run"
    seal = read(run / "complete.json")
    for name, digest in seal["artifacts"].items():
        assert sha(run / name) == digest, name
        assert (run / name).read_bytes() == (installed / name).read_bytes(), name
    assert (run / "complete.json").read_bytes() == (installed / "complete.json").read_bytes()
    owner = Path("/Users/yinming/Documents/Codex/2026-10-10/task-3/independent-query-delivery")
    handoff_path = owner / "handoff-batch2.json"
    assert sha(handoff_path) == "bbabb23beb6bde4ff0c6557122804f9edfa1dc1a54dfd1d68d6d87e71160b26f"
    handoff = read(handoff_path)
    receipts = {}
    for name in (
        "binding",
        "wheel",
        "sdist",
        "validation",
        "package_verification",
        "prior_batch_unchanged",
        "README",
    ):
        item = handoff[name]
        path = owner / item["path"]
        assert sha(path) == item["sha256"], name
        receipts[name] = ref(path)
    assert receipts["binding"]["sha256"] == seal["owner_binding_sha256"]
    assert receipts["wheel"]["sha256"] == seal["owner_wheel_sha256"]
    public = read(run / "public-products.json")
    for name, value in public["products"].items():
        filename = "C04-" + name + ".json"
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        assert hashlib.sha256(raw).hexdigest() == handoff["products"][filename]["sha256"]
        assert raw == (owner / "products" / filename).read_bytes()
    supplements = {}
    for name, item in handoff["supplements"].items():
        path = owner / item["path"]
        assert sha(path) == item["sha256"], name
        supplements[name] = dict(
            binding=ref(path),
            content=read(path),
            status="OWNER_REPORT_RECEIVED_NOT_AQ_SOURCE_CERTIFICATION",
        )
    report = read(run / "C04.json")
    comparisons = []
    for item in report["comparisons"]:
        assert item["case_verdict"] is None and item["historical_pit_certified"] is False
        comparisons.append(
            dict(
                name=item["owner_read_name"],
                parameters=item["request"]["parameters"],
                **item["computation"],
                historical_pit_certified=False,
                official_chain_verified=item["owner_metadata"]["official_chain_verified"],
                complete_membership_certified=item["owner_metadata"][
                    "complete_membership_certified"
                ],
                historical_name_verified=item["owner_metadata"]["historical_name_verified"],
            )
        )
    coverage = supplements["c03-coverage"]["content"]
    old = read(
        root / ".verification/round-one/independent-batch1-dev10/source-run/public-products.json"
    )
    rows = old["products"]["C03-history-sina-pingan"]["rows"]
    dates = {r["date"] for r in rows}
    assert len(rows) == coverage["raw_returned_rows"] == 1195
    assert min(dates) == coverage["first_in_window"]
    assert max(dates) == coverage["last_in_window"]
    assert not dates.intersection(coverage["calendar_comparison"]["missing_dates"])
    return dict(
        schema="aq.independent-index-review.v1",
        source_execution=ref(run / "complete.json"),
        installed_execution=ref(installed / "complete.json"),
        source_installed_identical=True,
        owner_handoff=ref(handoff_path),
        verified_owner_files=receipts,
        owner_installed_files_checked=public["owner_installed_files_checked"],
        public_products_verified=3,
        comparison_count=3,
        comparisons=comparisons,
        report=ref(run / "C04.json"),
        supplements=supplements,
        C03_prior_public_rows_crosscheck="1195 and endpoints unchanged; 14 owner-reported missing days absent",
        source_certification_promoted=False,
        new_JQ_runs=0,
        new_native_runs=0,
        new_external_requests=0,
        old_AD_environments_modified=False,
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    value = review(root)
    with (root / "docs/round-one/independent-index-review-dev12.json").open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(value["comparisons"], ensure_ascii=False))
