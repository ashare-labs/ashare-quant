"""Record the installed candidate's real content; never carries forward historical review."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import zipfile
from importlib import metadata
from importlib.resources import files
from io import BytesIO
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source", type=Path, help="Optional ashare-data git checkout to verify commit/tree against wheel")
    parser.add_argument("--commit", help="Actual candidate source commit; omit for uncommitted development")
    parser.add_argument("--tree", help="Actual source tree; omit for uncommitted development")
    args = parser.parse_args()
    from ashare_data.research.payloads import QUERY_CONTRACT_SHA256
    from ashare_data.research.portable import VERSION as PORTABLE_OWNER_VERSION
    from ashare_data.research.portable_types import canonical_hash

    root = Path(str(files("ashare_data")))
    inventory = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob("*")) if p.suffix in {".py", ".json", ".js"}}
    with zipfile.ZipFile(args.wheel) as archive:
        wheel_files = {name.removeprefix("ashare_data/"): hashlib.sha256(archive.read(name)).hexdigest()
                       for name in archive.namelist()
                       if name.startswith("ashare_data/") and Path(name).suffix in {".py", ".json", ".js"}}
    if inventory != wheel_files:
        raise SystemExit("Installed AD and supplied wheel differ; pin was not written")
    commit, tree = args.commit, args.tree
    if args.source is not None:
        ref = commit or "HEAD"
        resolved_commit = subprocess.check_output(
            ["git", "-C", str(args.source), "rev-parse", ref], text=True
        ).strip()
        resolved_tree = subprocess.check_output(
            ["git", "-C", str(args.source), "rev-parse", f"{resolved_commit}^{{tree}}"], text=True
        ).strip()
        if commit and commit != resolved_commit:
            raise SystemExit("Supplied --commit does not match resolved git commit")
        if tree and tree != resolved_tree:
            raise SystemExit("Supplied --tree does not match resolved git tree")
        commit, tree = resolved_commit, resolved_tree
        raw_tar = subprocess.check_output(["git", "-C", str(args.source), "archive", commit, "src/ashare_data"])
        source_files = {}
        with tarfile.open(fileobj=BytesIO(raw_tar)) as tar:
            for member in tar.getmembers():
                if member.isfile() and Path(member.name).suffix in {".py", ".json", ".js"}:
                    rel = member.name.removeprefix("src/ashare_data/")
                    source_files[rel] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
        if source_files != inventory:
            raise SystemExit("Commit tree in --source and supplied wheel differ; rebuild wheel from commit first")
    pin = dict(schema="aq.data-runtime.v1", version=metadata.version("ashare-data"),
               portable_owner_version=PORTABLE_OWNER_VERSION,
               commit=commit, tree=tree,
               source_verification="committed_candidate_content" if commit else "uncommitted_candidate_content",
               runtime_code_sha256=canonical_hash(inventory), query_contract_sha256=QUERY_CONTRACT_SHA256,
               wheel_sha256=hashlib.sha256(args.wheel.read_bytes()).hexdigest(), files=inventory,
               distribution_status="unpublished_candidate",
               contract_review=dict(status="not_reviewed", receipt_sha256=None,
                                    scope="data_production_contract_only", execution_permission=False))
    target = Path(__file__).resolve().parents[1] / "src/ashare_quant/providers/data-runtime.json"
    target.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k:v for k,v in pin.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
