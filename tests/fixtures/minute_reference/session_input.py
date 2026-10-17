"""Project the fixed AD full-session export without acquiring or repairing prices.

This deliberately bounded example consumes the independently reviewed AD delivery.
Its phase names are mapped explicitly; original records, values and unknown clocks
remain intact. The old research input supplies only its existing state/action scope.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

import ashare_quant.backtests.minute.bounded as research
from ashare_quant.backtests.minute.inputs import MinuteInputs
from tests.fixtures.minute_reference.compare import INPUT_SHA256, require

AD_COMMIT = "2b382e1113ce1e55a145d846c6a29f8555ad25ee"
AD_MANIFEST_SHA256 = "44b9de7194e1ad113252ff50fe16d725a4344300488d9555d423bad1796bebc6"
AD_WHEEL_SHA256 = "9e175a643ffe483c0f576d22490b0bf1c9360b961f0248addccba8382af77450"
AD_EXPORT_SHA256 = "393a44c5d9b2fd70fa156c53f9175350d68199575181ccc64a45b40594b86381"
FULL_INPUT_SHA256 = "3baad1cca4695ab896ea7f16556a3aeeb198ec2ff85809bebff6898e37aee0b5"
INHERITED = ("states", "actions", "action_coverage", "calendar", "state_replay", "source_refs")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_delivery(root):
    root = Path(root).resolve()
    require(sha(root / "MANIFEST.json") == AD_MANIFEST_SHA256, "AD delivery manifest changed")
    manifest = json.loads((root / "MANIFEST.json").read_bytes())
    require(manifest["ad_commit"] == AD_COMMIT, "AD source commit changed")
    for name, entry in manifest["files"].items():
        path = (root / name).resolve()
        require(path.is_relative_to(root), "AD delivery path escapes root")
        require(path.stat().st_size == entry["bytes"], "AD delivery size changed: " + name)
        require(sha(path) == entry["sha256"], "AD delivery hash changed: " + name)
    return len(manifest["files"])


def project(session_path, inherited_path):
    require(sha(session_path) == AD_EXPORT_SHA256, "Fixed AD session export changed")
    require(sha(inherited_path) == INPUT_SHA256, "Inherited state/action input changed")
    session = json.loads(Path(session_path).read_bytes())
    baseline = json.loads(Path(inherited_path).read_bytes())
    require(session["schema"] == "ad-tdx-minute-session.v1", "AD session schema")
    require(session["model"] == "tdx-full-session-retrospective.v1", "AD explicit model")
    document = copy.deepcopy(baseline)
    document.update(
        schema=research.SCHEMA_FULL,
        case_id=research.PROFILE_FULL,
        producer=session["producer"],
        sessions=session["sessions"],
        minute=copy.deepcopy(session["minute"]),
        session_observations=copy.deepcopy(session["session_observations"]),
        limitations=list(research.FULL_ASSUMPTIONS) + session["limitations"],
    )
    document["research"].update(
        profile=research.PROFILE_FULL,
        assumptions=list(research.FULL_ASSUMPTIONS),
        tdx_reports=session["source_reports"],
        ad_session_delivery=dict(
            commit=AD_COMMIT,
            manifest_sha256=AD_MANIFEST_SHA256,
            wheel_sha256=AD_WHEEL_SHA256,
            export_sha256=AD_EXPORT_SHA256,
            phase_projection="AD closing_auction_candidate to AQ declared research stage",
        ),
    )
    for index, row in enumerate(document["minute"]):
        if row["phase"] != "closing_auction_candidate":
            continue
        require(
            row["phase_basis"] == "explicit_source_label_research_model"
            and row["phase_verified"] is False
            and row["historical_pit"] is False
            and row["strict_execution_admitted"] is False,
            "AD closing stage is not the reviewed unverified research declaration",
        )
        row["session_phase"] = "closing_auction"
        row["phase_evidence"] = dict(
            model="ad-declared-closing-stage.v1",
            source_label=row["source_label"],
            manifest_sha256=row["evidence"]["manifest_sha256"],
            source_refs=[f"sha256:{AD_EXPORT_SHA256}#/minute/{index}/phase"],
            historical_phase_verified=False,
        )
        require(all(row[k] == v for k, v in session["minute"][index].items()), "AD row changed")
    require(all(document[k] == baseline[k] for k in INHERITED), "State/action scope changed")
    return document


def write_input(session_path, inherited_path, output):
    output = Path(output)
    document = project(session_path, inherited_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(document, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stream.write("\n")
    data = MinuteInputs(output, accept_assumed=True, accept_state_replay=True, accept_research=True)
    require(sha(output) == FULL_INPUT_SHA256, "Reviewed projection bytes changed")
    return dict(
        input_sha256=sha(output),
        ad_export_sha256=AD_EXPORT_SHA256,
        execution_rows=len(data.bars),
        history_rows=len(data.session_data.history),
        zero_volume_rows=sum(float(row["volume"]) == 0 for row in document["minute"]),
        inherited_state_action_scope_unchanged=True,
        historical_pit_verified=False,
        strict_execution_admitted=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ad-package", type=Path, required=True)
    parser.add_argument("--inherited-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = verify_delivery(args.ad_package)
    result = write_input(args.ad_package / "session-source.json", args.inherited_input, args.output)
    print(json.dumps(dict(verified_ad_files=count, **result), indent=2))


if __name__ == "__main__":
    main()
