"""Prepare, verify, apply or roll back saved-strategy import migrations offline."""

import argparse
import difflib
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ashare_quant.deployment.runtime import deployment_lock
from ashare_quant.strategies.files import StrategyFiles, parts
from ashare_quant.strategies.imports import migrate_source

SCHEMA = "aquant-import-migration.v1"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def inventory(files, folder=""):
    for item in files.list(folder)["entries"]:
        if item["kind"] == "directory":
            yield from inventory(files, item["path"])
        else:
            yield item["path"]


def prepare(state_root, output):
    state_root, output = Path(state_root).absolute(), Path(output).absolute()
    files = StrategyFiles(state_root)
    if output.resolve().is_relative_to(files.root.resolve()):
        raise ValueError("Migration bundle must be outside the saved-strategy directory")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    entries = []
    for name in inventory(files):
        raw = files.read(name)["source"].encode("utf-8")
        try:
            candidate, report = migrate_source(raw)
        except (SyntaxError, UnicodeError, ValueError) as exc:
            candidate = raw
            report = {"status": "review_required", "issues": [str(exc)], "changes": 0}
        for directory, content in (("original", raw), ("candidate", candidate)):
            path = output / directory / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(content)
        diff = "".join(difflib.unified_diff(
            raw.decode("utf-8").splitlines(keepends=True),
            candidate.decode("utf-8").splitlines(keepends=True),
            fromfile="original/" + name, tofile="candidate/" + name,
        ))
        path = output / "diff" / (name + ".diff")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(diff)
        entries.append({"path": name, "before_sha256": sha(raw),
                        "after_sha256": sha(candidate), **report})
    manifest = {"schema": SCHEMA, "state_root": str(state_root), "files": entries}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def bundle_file(bundle, kind, name, expected):
    parts(name, file=True)
    path = bundle / kind / name
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Migration bundle must not contain symbolic links")
        if parent == bundle:
            break
    raw = path.read_bytes()
    if sha(raw) != expected:
        raise ValueError(f"Migration bundle digest mismatch: {kind}/{name}")
    return raw


def verify(bundle):
    bundle = Path(bundle).absolute()
    if bundle.is_symlink() or (bundle / "manifest.json").is_symlink():
        raise ValueError("Migration bundle must not contain symbolic links")
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest["schema"] != SCHEMA:
        raise ValueError("Unknown migration bundle schema")
    files = StrategyFiles(Path(manifest["state_root"]))
    seen, current = set(), []
    for entry in manifest["files"]:
        name = entry["path"]
        if name in seen:
            raise ValueError("Duplicate migration path")
        seen.add(name)
        before = bundle_file(bundle, "original", name, entry["before_sha256"])
        after = bundle_file(bundle, "candidate", name, entry["after_sha256"])
        if entry["status"] == "review_required" and after != before:
            raise ValueError("Review-required entries must retain the original bytes")
        if entry["status"] != "review_required":
            expected, report = migrate_source(before)
            if after != expected or report["status"] != entry["status"]:
                raise ValueError(f"Candidate is not the verified import-only migration: {name}")
        digest = sha(files.read(name)["source"].encode("utf-8"))
        if digest not in {entry["before_sha256"], entry["after_sha256"]}:
            raise ValueError(f"Saved strategy changed since preparation: {name}")
        current.append({"path": name, "sha256": digest,
                        "state": "original" if digest == entry["before_sha256"] else "migrated"})
    if seen != set(inventory(files)):
        raise ValueError("Saved strategy inventory changed; prepare a fresh bundle")
    return manifest, current


def require_quiesced(state_root, control_db, maintenance):
    if not maintenance:
        raise ValueError("Stop API/editor writes and workers, then explicitly pass --maintenance")
    path = Path(control_db).absolute()
    if path.parent != Path(state_root) or not path.is_file() or path.is_symlink():
        raise ValueError("Use the existing control database beside this strategies directory")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        pending = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('succeeded','failed','cancelled')"
        ).fetchone()[0]
    if pending:
        raise ValueError("Unfinished tasks exist; do not migrate queued, running or recovering work")


def publish(bundle, *, rollback=False, control_db, maintenance=False):
    manifest, current = verify(bundle)  # Validate every item before the first write.
    if not rollback and any(e["status"] == "review_required" for e in manifest["files"]):
        raise ValueError("Manual review required; no strategies were written")
    with deployment_lock(SimpleNamespace(control_db=Path(control_db).absolute()), exclusive=True):
        require_quiesced(manifest["state_root"], control_db, maintenance)
        files, bundle = StrategyFiles(manifest["state_root"]), Path(bundle).absolute()
        for entry, observed in zip(manifest["files"], current, strict=True):
            target = "before_sha256" if rollback else "after_sha256"
            if observed["sha256"] == entry[target]:
                continue
            # Recheck immediately before each atomic publication. Maintenance also
            # excludes concurrent editors; interrupted operations are safely resumable.
            if sha(files.read(entry["path"])["source"].encode("utf-8")) != observed["sha256"]:
                raise ValueError("Strategy changed during migration; stop and verify the bundle")
            raw = bundle_file(bundle, "original" if rollback else "candidate",
                              entry["path"], entry[target])
            files.save(entry["path"], raw.decode("utf-8"), overwrite=True)
    _, current = verify(bundle)
    return {"operation": "rollback" if rollback else "apply", "files": current}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    command = commands.add_parser("prepare", help="Read saved files; write a new review bundle only")
    command.add_argument("--state-root", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    for name in ("verify", "apply", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--bundle", required=True, type=Path)
        if name != "verify":
            command.add_argument("--control-db", required=True, type=Path)
            command.add_argument("--maintenance", action="store_true",
                                 help="Operator confirms API/editor writes and workers are stopped")
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            result = prepare(args.state_root, args.output)
        elif args.operation == "verify":
            _, current = verify(args.bundle)
            result = {"operation": "verify", "files": current}
        else:
            result = publish(args.bundle, rollback=args.operation == "rollback",
                             control_db=args.control_db, maintenance=args.maintenance)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
