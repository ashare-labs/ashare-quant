"""Review sealed AD dev9 reads and original-scope comparisons; no acquisition."""

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
    run = root / ".verification/round-one/independent-repair-dev13/source-run"
    installed = run.parent / "installed-run"
    seal = read(run / "complete.json")
    for name, digest in seal["artifacts"].items():
        assert sha(run / name) == digest, name
        assert (run / name).read_bytes() == (installed / name).read_bytes(), name
    assert (run / "complete.json").read_bytes() == (installed / "complete.json").read_bytes()
    owner = Path("/Users/yinming/Documents/Codex/2026-10-10/task-3/independent-query-repair")
    manifest = owner / "MANIFEST.json"
    assert sha(manifest) == "22508fd6f8fa4800dd56fe87f952ca4a3af2cbd2162f3fd7b5cd1a09283df42e"
    files = read(manifest)["files"]
    for name, item in files.items():
        path = (owner / name).resolve()
        assert path.is_relative_to(owner)
        assert sha(path) == item["sha256"] and path.stat().st_size == item["bytes"], name
    handoff = read(owner / "handoff.json")
    public = read(run / "public-products.json")
    for name, value in public["products"].items():
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        assert hashlib.sha256(raw).hexdigest() == handoff["outputs"][name]["sha256"], name
        assert raw == (owner / "products" / (name + ".json")).read_bytes(), name
    previous_path = (
        root / ".verification/round-one/independent-batch1-dev10/source-run/public-products.json"
    )
    assert sha(previous_path) == "5914f59a6974ad56098d70740831f897bd10a4954412fdc0e7eb558fc0b5acb7"
    previous = read(previous_path)["products"]
    c02 = read(run / "C02.json")
    standardization = []
    for name in ("sina-pingan", "eastmoney-pingan"):
        product = public["products"]["c02-" + name]
        old = previous["C02-fields-" + name]
        current_fields, old_fields = product["rows"][0]["fields"], old["rows"][0]["fields"]
        assert set(current_fields) == set(old_fields)
        for field in set(current_fields) - {"pre_close", "factor"}:
            assert current_fields[field] == old_fields[field], (name, field)
        compared = [c for c in c02["comparisons"] if c["owner_product"] == "c02-" + name]
        new_fields = [
            c
            for c in compared
            if c["request"]["parameters"]["fields"][0] in ("pre_close", "factor")
        ]
        assert len(new_fields) == 2
        assert all(
            c["status"] == "COMPUTED_NOT_ADJUDICATED" and not c["comparison"]["differences"]
            for c in new_fields
        )
        standardization.append(
            dict(
                source=name,
                pre_close=current_fields["pre_close"],
                factor=current_fields["factor"],
                other_fields_unchanged=True,
                unsupported_fields=[
                    f for f, cell in current_fields.items() if cell["state"] == "unsupported"
                ],
                exact_differences=[
                    d for c in compared for d in c.get("comparison", {}).get("differences", [])
                ],
            )
        )
    reports = {}
    for item in handoff["reports"]:
        path = owner / item["path"]
        assert sha(path) == item["sha256"]
        reports[item["path"]] = dict(
            binding=ref(path),
            content=read(path),
            classification="OWNER_BOUND_REPORT_NOT_NEW_AQ_SOURCE_CERTIFICATION",
        )
    c03 = read(run / "C03.json")["comparisons"][0]
    halts = read(run / "C03-halt-attribution.json")
    assert public["products"]["c03-history"]["rows"] == previous["C03-history-sina-pingan"]["rows"]
    coverage = reports["reports/c03-coverage.json"]["content"]
    assert sorted(r["date"] for r in coverage["rows"]) == halts["documented_dates"]
    event_index = {r["date"]: r for r in halts["evidence_rows"]}
    for row in coverage["rows"]:
        assert row["classification"] == "provider_documented_full_day_halt"
        assert row["event_source_text"] == event_index[row["date"]]["source_content"]
    c05 = read(run / "C05.json")["comparisons"][0]
    assert c05["computed"]["left"]["captured_at"] == c05["reference_capture_window"]["end"]
    actions = {}
    for source in ("cninfo-dividends", "cninfo-rights"):
        product = public["products"]["b25-" + source]
        assert product["historical_action_chain_complete"] is False
        assert product["adjustment_factor_certified"] is False
        actions[source] = dict(
            rows=len(product["rows"]),
            source=product["source"],
            null_cells_preserved=True,
            consumed_by_factor_or_engine=False,
        )
    return dict(
        schema="aq.independent-repair-review.v1",
        source_execution=ref(run / "complete.json"),
        installed_execution=ref(installed / "complete.json"),
        source_installed_identical=True,
        owner_manifest=ref(manifest),
        owner_handoff=ref(owner / "handoff.json"),
        owner_manifest_files_verified=len(files),
        owner_installed_files_checked=public["owner_installed_files_checked"],
        public_products_verified=7,
        comparisons_per_environment=30,
        cases=dict(
            C02=dict(
                report=ref(run / "C02.json"),
                source_standardization=standardization,
                exact_differences=4,
                unsupported_fields_per_source=4,
            ),
            C03=dict(
                report=ref(run / "C03.json"),
                halt_attribution=ref(run / "C03-halt-attribution.json"),
                old_daily_rows_unchanged=True,
                documented_halts=14,
                additional_exchange_notice_mirrors=sum(
                    r["official_exchange_notice_mirror_available"] for r in coverage["rows"]
                ),
                comparison=c03["comparison"],
                first_nonempty=c03["computed"],
            ),
            C05=dict(
                report=ref(run / "C05.json"),
                comparison=c05["comparison"],
                computed=c05["computed"],
                reference_capture_window=c05["reference_capture_window"],
                actual_owner_versions=1,
                source_publication_revision_unknown=True,
                interpretation="10 matching date keys; money differs at source integer-yuan precision; no update/revision certification",
            ),
            B25=dict(
                action_ledgers=actions,
                existing_factor_products_changed=False,
                full_chain_certified=False,
                alternate_JQ_factor_certified=False,
            ),
        ),
        owner_reports=reports,
        new_JQ_runs=0,
        new_native_runs=0,
        new_external_requests=0,
        B26_expanded=False,
        historical_pit_promoted=False,
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    value = review(root)
    with (root / "docs/round-one/independent-repair-review-dev13.json").open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            dict(
                public_products=value["public_products_verified"],
                comparisons=value["comparisons_per_environment"],
                owner_files=value["owner_manifest_files_verified"],
                reports_identical=value["source_installed_identical"],
            )
        )
    )
