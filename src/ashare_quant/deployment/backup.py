"""Verified directory bundles for stopped deployments, including native artifacts.

Only the new deployment manifest is relocated. Frozen inputs, provenance strings,
task manifests, checkpoints and receipts are copied without content rewriting.
"""

import copy
import fcntl
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import tempfile
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path, PurePosixPath

from ashare_quant.contracts import DomainError
from ashare_quant.deployment.configuration import REFERENCE_KINDS, resolve
from ashare_quant.deployment.runtime import deployment_lock
from ashare_quant.storage.lifecycle import database_lock

BUNDLE_SCHEMA = "ashare-backup.v1"
SOURCE_ROLES = {"market", "financial", "factor", "factors", "reference", "market_evidence"}


def fail(code, message):
    raise DomainError(code, message, 409)


def digest_file(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        fail("BACKUP_UNSUPPORTED_FILE", f"Only regular files may be bundled: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def files(root):
    """Never follow symlinks or silently omit unknown non-regular artifacts."""
    if root.is_symlink():
        fail("BACKUP_SYMLINK", f"Symlink requires an explicit regular asset path: {root}")
    if root.is_file():
        signature(root)
        return [root]
    if not root.is_dir():
        fail("BACKUP_MISSING_INPUT", f"Required asset is unavailable: {root}")
    result = []
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(parent) / name
            if path.is_symlink():
                fail("BACKUP_SYMLINK", f"Directory symlink is not portable: {path}")
        for name in names:
            path = Path(parent) / name
            signature(path)
            result.append(path)
    return sorted(result)


def runtime_identity():
    """Machine paths are descriptive only; hashes and dependency versions bind execution."""
    source = {}
    for name in ("ashare_quant", "ashare_data"):
        root = Path(next(iter(find_spec(name).submodule_search_locations)))
        source[name] = {
            str(p.relative_to(root)): digest_file(p) for p in files(root)
            if "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
        }
    lock = Path(__file__).resolve().parent.parent / "native-runtime-lock.json"
    expected = json.loads(lock.read_text())
    dependencies = {}
    for name in sorted(set(expected) | {"ashare-quant", "ashare-data"}):
        if name != "python":
            try:
                dependencies[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                dependencies[name] = "missing"
    return {"python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "dependencies": dependencies,
            "sources": source}


def _runtime_payload(destination, identity):
    target = destination / "runtime"
    for package, entries in identity["sources"].items():
        source = Path(next(iter(find_spec(package).submodule_search_locations)))
        for relative, expected in entries.items():
            path = target / "python" / package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, path)
            if digest_file(path) != expected:
                fail("BACKUP_RUNTIME_CHANGED", "Runtime sources changed during backup")
        distribution = metadata.distribution(package.replace("_", "-"))
        # Preserve metadata for owner package/runtime verification under PYTHONPATH.
        for entry in distribution.files or ():
            if ".dist-info" in entry.parts[0]:
                source_file = Path(distribution.locate_file(entry))
                if source_file.is_file():
                    path = target / "python" / str(entry)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, path)
    requirements = "".join(f"{name}=={version}\n" for name, version in
                           identity["dependencies"].items()
                           if name not in {"ashare-quant", "ashare-data"})
    (target / "requirements.lock").write_text(requirements)
    (target / "identity.json").write_bytes(canonical(identity))
    (target / "RESTORE.md").write_text(
        "Use the recorded Python version and architecture. Create a new virtual environment, "
        "then install runtime/requirements.lock using the saved wheelhouse or a verified "
        "package cache. The bundle includes the exact AQ/AD package sources and metadata "
        "under runtime/python. Set PYTHONPATH to that absolute directory before invoking "
        "python -m ashare_quant.deployment verify BACKUP and restore BACKUP EMPTY_DIRECTORY. "
        "If delivery wheels are registered as required assets, install those exact wheels "
        "instead. verify and restore reject a different runtime. No interpreter or third-party "
        "dependency wheelhouse is implied to be in this bundle. Use --expected-sha256 with "
        "the separately retained manifest digest to detect replacement of the manifest.\n"
    )


def _plan(config, include_source_assets):
    roots, mapped = [], {}
    required_assets = set(config.document["profiles"][config.profile].get("required_assets", []))

    def add(name, source, target, *, required=False, source_asset=False):
        if not source.exists():
            if required:
                fail("BACKUP_MISSING_INPUT", f"Required asset is missing: {source}")
            return
        for entry in roots:
            if source == entry["source"] or (
                entry["source"].is_dir() and source.is_relative_to(entry["source"])
            ):
                mapped[name] = str(Path(entry["target"]) / source.relative_to(entry["source"]))
                return
        mapped[name] = target
        roots.append({"source": source, "target": target, "source_asset": source_asset})

    add("control_db", config.control_db, "payload/state/control.sqlite3", required=True)
    add("tasks", config.control_db.with_name(config.control_db.name + ".tasks"),
        "payload/state/control.sqlite3.tasks")
    for name in ("engines", "inputs", "checkpoints", "receipts", "strategies"):
        add(name, config.control_db.parent / name, "payload/state/" + name)
    add("paper_root", config.paper_root, "payload/paper", required=True)
    omitted = []
    for asset in config.assets:
        spec = config.document["assets"][asset.name]
        selected = asset.name in config.selected_assets
        reference_asset = asset.kind in REFERENCE_KINDS
        source_asset = asset.kind == "local_daily" or asset.role in SOURCE_ROLES or reference_asset
        protected = (spec.get("protected") or asset.retention in {"protected_external", "frozen"}
                     or asset.role == "protected_external")
        temporary = asset.retention in {"temporary", "discard"} or asset.role == "temporary"
        if selected and (protected or temporary):
            fail("BACKUP_PROFILE_ASSET_INVALID", f"Required input cannot be excluded/protected: {asset.name}")
        if asset.kind == "local_daily" and asset.name in required_assets and not include_source_assets:
            fail("BACKUP_REQUIRED_SOURCE_REQUIRES_OPT_IN",
                 f"Required whole source {asset.name} needs --include-source-assets; "
                 "default state backup never silently copies or omits a required main library")
        include = (selected and (not source_asset or reference_asset or asset.name in required_assets)
                   or include_source_assets and not protected and not temporary)
        if include:
            # Asset names are logical identifiers, never archive paths.
            target = "payload/assets/" + hashlib.sha256(asset.name.encode()).hexdigest()[:24]
            add("asset:" + asset.name, asset.path, target, required=True, source_asset=source_asset)
        else:
            omitted.append({"name": asset.name, "path": str(asset.path), "role": asset.role,
                            "reason": "protected_external" if protected else "temporary" if temporary
                            else "external_source" if source_asset else "other_profile"})
    return roots, mapped, omitted


def reference_asset_checks(config, *, relocated=None):
    """Read immutable source packages through AD, including execution/base pairing.

    Used by doctor and by the backup verifier. A relocated mapping contains only
    package roots inside the bundle, so verification never falls back to an old
    machine path. No Store, source query or package writer is constructed.
    """
    specs = {}
    for asset in config.assets:
        selected = asset.name in (relocated if relocated is not None else config.selected_assets)
        if selected and asset.kind in REFERENCE_KINDS:
            specs[asset.name] = {
                "kind": asset.kind, "path": (relocated[asset.name] if relocated is not None else asset.path),
                "binding": asset.binding, "binding_sha256": asset.binding_sha256,
            }
    return _reference_checks(specs)


def _reference_checks(specs):
    if not specs:
        return {}
    from ashare_data import (
        CorporateHistoryView,
        DailyExecutionView,
        DailyResearchView,
        HistoricalMarketView,
    )
    from ashare_data.core.model import DataError

    constructors = {"daily_research": DailyResearchView, "historical_market": HistoricalMarketView,
                    "corporate_history": CorporateHistoryView}
    results, research = {}, {}
    # Base snapshots must be loaded before their independently pinned supplements.
    for name, spec in sorted(specs.items(), key=lambda item: item[1]["kind"] == "daily_execution"):
        binding = Path(spec["path"]) / _relative(spec["binding"] or "binding.json")
        result = {"kind": spec["kind"], "path": str(spec["path"]), "binding_path": str(binding),
                  "binding_sha256": spec["binding_sha256"], "ready": False}
        try:
            if not binding.is_file():
                raise DomainError("REFERENCE_BINDING_MISSING", f"Missing binding: {binding}", 409)
            if binding.stat().st_size > 2 * 1024 * 1024:
                raise DomainError("REFERENCE_BINDING_INVALID", "Binding exceeds 2 MiB", 409)
            if digest_file(binding) != spec["binding_sha256"]:
                raise DomainError("REFERENCE_BINDING_CHANGED", f"Binding pin differs: {binding}", 409)
            if spec["kind"] == "daily_execution":
                base_pin = json.loads(binding.read_bytes())["research_binding_sha256"]
                if base_pin not in research:
                    raise DomainError("REFERENCE_DEPENDENCY_MISSING",
                                      "Execution package needs its valid pinned research asset", 409)
                view = DailyExecutionView(research[base_pin], binding,
                                          expected_binding_sha256=spec["binding_sha256"])
                result["research_binding_sha256"] = base_pin
            else:
                view = constructors[spec["kind"]](binding, expected_binding_sha256=spec["binding_sha256"])
                if spec["kind"] == "daily_research":
                    research[spec["binding_sha256"]] = view
            result["ready"] = True
        except (DataError, DomainError, OSError, ValueError, KeyError, TypeError) as exc:
            result.update(error=getattr(exc, "code", "REFERENCE_PACKAGE_INVALID"), message=str(exc))
        results[name] = result
    return results


def _require_reference_checks(checks):
    invalid = {name: check for name, check in checks.items() if not check["ready"]}
    if invalid:
        raise DomainError("BACKUP_REFERENCE_INVALID", "Required reference package is missing or invalid", 409,
                          details={"assets": invalid})


def _bundle_reference_checks(manifest, directory):
    """All selected small source packages and explicit required inputs must exist."""
    config = manifest["configuration"]
    profile = config["profiles"][manifest["profile"]]
    required = set(profile.get("required_assets", []))
    for reference in profile.get("reference_sets", {}).values():
        required.update(value for key, value in reference.items() if key.endswith("_asset"))
    missing = sorted(name for name in required if "asset:" + name not in manifest["paths"])
    if missing:
        fail("BACKUP_REQUIRED_ASSET_MISSING", f"Bundle omitted explicitly required assets: {missing}")
    specs = {}
    for name, asset in config["assets"].items():
        target = manifest["paths"].get("asset:" + name)
        if asset["kind"] in REFERENCE_KINDS and target is not None:
            specs[name] = {**asset, "path": directory / _relative(target),
                           "binding": asset.get("binding", "binding.json")}
    checks = _reference_checks(specs) if specs else {}
    _require_reference_checks(checks)
    return checks


def _is_sqlite(path):
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _snapshot(source, destination):
    """Read-only capture of original DB/WAL; SQLite backup merges the private copy."""
    with tempfile.TemporaryDirectory(prefix="aq-db-snapshot-") as folder:
        local = Path(folder) / "snapshot.sqlite3"
        for suffix in ("", "-wal", "-journal"):
            candidate = Path(str(source) + suffix)
            if candidate.exists():
                signature(candidate)
                shutil.copyfile(candidate, Path(str(local) + suffix))
        with closing(sqlite3.connect(local)) as db, closing(sqlite3.connect(destination)) as out:
            db.backup(out)
            if out.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                fail("BACKUP_DATABASE_CORRUPT", f"SQLite integrity failed: {source}")
            if out.execute("PRAGMA foreign_key_check").fetchone() is not None:
                fail("BACKUP_DATABASE_CORRUPT", f"SQLite references failed: {source}")


def _not_active(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("runs", "tasks", "paper_days"):
            if table in tables:
                active = db.execute(
                    f"SELECT count(*) FROM {table} WHERE status IN ('running','cancelling')"
                ).fetchone()[0]
                if active:
                    fail("BACKUP_ACTIVE_TASKS", f"{table} has active/unreconciled writers; stop and reconcile first")


def _state_artifacts(config):
    """Refuse known broken checkpoint/input links before calling a bundle complete."""
    def checked(path, expected=None):
        signature(path)
        if expected is not None and digest_file(path) != expected:
            fail("BACKUP_STATE_INCONSISTENT", f"Control record and artifact differ: {path}")

    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    with closing(sqlite3.connect(config.control_db.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM tasks"):
            root = config.control_db.with_name(config.control_db.name + ".tasks") / _relative(row["job_id"])
            document = json.loads(row["manifest"])
            checked(root / "accepted-manifest.json", row["manifest_sha256"])
            if hashlib.sha256(encoded(document)).hexdigest() != row["manifest_sha256"]:
                fail("BACKUP_STATE_INCONSISTENT", "Task manifest identity differs")
            checked(root / "session.json", document["session_file_sha256"])
            if row["checkpoint_sha256"]:
                checked(root / "native.chk", row["checkpoint_sha256"])
            for name, value in document.get("files", {}).items():
                checked(root / _relative(name), value)
            if document.get("schema_version", "").startswith("m2-job-manifest"):
                for name, field in (("parameters.json", "parameters_sha256"),
                                    ("data-binding.json", "data_binding_sha256"),
                                    ("strategy-source.py", "python_source_sha256")):
                    if document.get(field):
                        checked(root / name, document[field])
                checked(root / "strategy.json")
                checked(root / "execution-scope.json")
        for row in db.execute("SELECT run_id FROM runs WHERE native_started=1"):
            root = config.control_db.parent / "engines" / _relative(row["run_id"])
            if not root.is_dir() or not files(root):
                fail("BACKUP_MISSING_INPUT", "A started native run is missing its engine directory")
    paper_db = config.paper_root / "control.sqlite3"
    if paper_db.exists():
        with closing(sqlite3.connect(paper_db.as_uri() + "?mode=ro", uri=True)) as db:
            for sid, day, checkpoint, report in db.execute(
                "SELECT session_id,day,checkpoint_sha,report_sha FROM paper_days WHERE status='committed'"
            ):
                root = config.paper_root / _relative(sid) / _relative(day)
                checked(root / "native.chk", checkpoint)
                checked(root / "report.json", report)


def _lock_existing(stack, path):
    stream = stack.enter_context(path.open("rb"))
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise DomainError("BACKUP_WRITER_BUSY", f"Writer is active: {path}", 409) from exc


def create_backup(config, destination, *, include_source_assets=False):
    destination = Path(destination).expanduser()
    if destination.exists() or destination.is_symlink():
        fail("BACKUP_EXISTS", "Backup destination must be a new directory")
    destination = destination.resolve()
    roots, mapped, omitted = _plan(config, include_source_assets)
    for entry in roots:
        if destination == entry["source"] or destination.is_relative_to(entry["source"]):
            fail("BACKUP_PATH_CONFLICT", "Backup must be outside every included source tree")
    checks = reference_asset_checks(config, relocated={a.name: a.path for a in config.assets
                                                      if "asset:" + a.name in mapped})
    _require_reference_checks(checks)
    identity = runtime_identity()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)  # create-only; incomplete bundles never receive manifest.json
    try:
        with ExitStack() as stack:
            stack.enter_context(deployment_lock(config, exclusive=True))
            stack.enter_context(database_lock(config.control_db, exclusive=True))
            paths = {p: entry for entry in roots for p in files(entry["source"])}
            for path in sorted(paths):
                if path.suffix == ".lock":
                    _lock_existing(stack, path)
            for asset in config.assets:
                if asset.kind == "local_daily" and "asset:" + asset.name in mapped:
                    if asset.path / "writer.lock" not in paths:
                        fail("BACKUP_SOURCE_LOCK_MISSING", "LocalDaily source must have its persistent writer.lock")
            databases = {p for p in paths if _is_sqlite(p)}
            # Reserve mutable control/publication DBs after excluding process writers.
            # Full-source assets remain read-only under their owner writer.lock.
            for path in sorted(databases):
                if not paths[path]["source_asset"]:
                    connection = stack.enter_context(closing(sqlite3.connect(
                        path.as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None
                    )))
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        raise DomainError("BACKUP_WRITER_BUSY", f"SQLite writer is active: {path}", 409) from exc
            _not_active(config.control_db)
            paper_db = config.paper_root / "control.sqlite3"
            if paper_db.exists():
                _not_active(paper_db)
            _state_artifacts(config)
            # Reservations may create empty SQLite sidecars. Capture that stable inventory now.
            paths = {p: entry for entry in roots for p in files(entry["source"])}
            before = {p: signature(p) for p in paths if not str(p).endswith("-shm")}
            source_hashes = {}
            for entry in roots:
                source, target = entry["source"], destination / entry["target"]
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                for path in files(source):
                    output = target / path.relative_to(source) if source.is_dir() else target
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if any(str(path) == str(db) + suffix for db in databases
                           for suffix in ("-wal", "-shm", "-journal")):
                        continue
                    if path in databases:
                        _snapshot(path, output)
                    else:
                        original = digest_file(path)
                        shutil.copyfile(path, output)
                        if digest_file(output) != original or digest_file(path) != original:
                            fail("BACKUP_SOURCE_CHANGED", f"Asset changed while copying: {path}")
                        source_hashes[str(path)] = original
            after_paths = {p for entry in roots for p in files(entry["source"])
                           if not str(p).endswith("-shm")}
            if set(before) != after_paths or any(signature(p) != before[p] for p in before):
                fail("BACKUP_SOURCE_CHANGED", "Source inventory changed; the bundle is not consistent")
            # External evidence may not cooperate with owner locks. Verify full hashes a second time.
            if any(digest_file(Path(p)) != value for p, value in source_hashes.items()):
                fail("BACKUP_SOURCE_CHANGED", "External input bytes changed during backup")
            _runtime_payload(destination, identity)
            if runtime_identity() != identity:
                fail("BACKUP_RUNTIME_CHANGED", "Runtime changed while backup was in progress")
            (destination / "original-deployment.json").write_bytes(config.deployment.read_bytes())
            manifest = {
                "schema": BUNDLE_SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                "scope": "source-assets-and-state" if include_source_assets else "state",
                "profile": config.profile, "runtime": identity, "paths": mapped,
                "configuration": config.document,
                "original_assets": {a.name: str(a.path) for a in config.assets},
                "omitted_assets": omitted,
                "source_tree_semantics": "owner locks, SQLite snapshots, two complete input inventories and hashes",
                "files": {str(p.relative_to(destination)): {
                    "size": p.stat().st_size, "sha256": digest_file(p)
                } for p in files(destination)},
            }
            manifest["reference_assets"] = _bundle_reference_checks(manifest, destination)
            raw = canonical(manifest)
            (destination / "manifest.json").write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            (destination / "manifest.sha256").write_text(digest + "\n")
        return {"backup": str(destination), "manifest_sha256": digest,
                "scope": manifest["scope"], "files": len(manifest["files"]),
                "omitted_assets": omitted}
    except BaseException:
        # This is solely the new exclusive destination allocated by this invocation.
        shutil.rmtree(destination)
        raise


def _relative(value):
    if not isinstance(value, str):
        fail("BACKUP_INVALID_PATH", "Bundle paths must be strings")
    path = PurePosixPath(value)
    if (not value or value == "." or path.is_absolute() or ".." in path.parts
            or "\\" in value or value != str(path)):
        fail("BACKUP_INVALID_PATH", f"Bundle path escapes its root: {value}")
    return Path(value)


def verify_backup(directory, *, expected_sha256=None, check_runtime=True):
    directory = Path(directory).expanduser().resolve()
    all_files = files(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file() or not (directory / "manifest.sha256").is_file():
        fail("BACKUP_INCOMPLETE", "Complete manifest and digest are required")
    digest = digest_file(manifest_path)
    if digest != (directory / "manifest.sha256").read_text().strip() or (
        expected_sha256 is not None and digest != expected_sha256
    ):
        fail("BACKUP_MANIFEST_CHANGED", "Manifest digest does not match the retained receipt")
    try:
        manifest = json.loads(manifest_path.read_bytes())
        if manifest["schema"] != BUNDLE_SCHEMA:
            fail("BACKUP_SCHEMA", "Unsupported backup schema")
        expected_files = {_relative(p) for p in manifest["files"]}
        actual = {p.relative_to(directory) for p in all_files} - {
            Path("manifest.json"), Path("manifest.sha256")
        }
        if actual != expected_files:
            fail("BACKUP_INVENTORY_CHANGED", "Missing or extra backup files")
        for name, expected in manifest["files"].items():
            path = directory / _relative(name)
            if path.stat().st_size != expected["size"] or digest_file(path) != expected["sha256"]:
                fail("BACKUP_FILE_CHANGED", f"Backup content differs: {name}")
        for name in manifest["paths"].values():
            path = directory / _relative(name)
            if not path.exists():
                fail("BACKUP_INCOMPLETE", f"Required bundle root is missing: {name}")
        _bundle_reference_checks(manifest, directory)
        if check_runtime and manifest["runtime"] != runtime_identity():
            fail("BACKUP_RUNTIME_MISMATCH", "Use this backup's exact Python, dependency and AQ/AD source versions")
    except (ValueError, KeyError, TypeError) as exc:
        raise DomainError("BACKUP_INVALID_MANIFEST", "Malformed backup manifest", 409) from exc
    return manifest


def restore_backup(directory, destination, *, expected_sha256=None):
    source = Path(directory).expanduser().resolve()
    manifest = verify_backup(source, expected_sha256=expected_sha256)
    destination = Path(destination).expanduser()
    if destination.is_symlink() or destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        fail("RESTORE_DESTINATION_NOT_EMPTY", "Restore requires a new or empty directory")
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        fail("BACKUP_PATH_CONFLICT", "Backup and restored directory must be separate")
    destination.mkdir(parents=True, exist_ok=True)
    # Preserve the bundle layout; create-only files prevent accidental overwrite.
    for name in manifest["files"]:
        relative = _relative(name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with (source / relative).open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        if digest_file(target) != manifest["files"][name]["sha256"]:
            fail("RESTORE_FILE_CHANGED", f"Restore verification failed: {name}")
    for name in manifest["paths"].values():
        if (source / _relative(name)).is_dir():
            (destination / name).mkdir(parents=True, exist_ok=True)
    document = copy.deepcopy(manifest["configuration"])
    document["data_root"] = "."
    document["profiles"] = {manifest["profile"]: document["profiles"][manifest["profile"]]}
    profile = document["profiles"][manifest["profile"]]
    profile["control_db"] = manifest["paths"]["control_db"]
    profile["paper_root"] = manifest["paths"]["paper_root"]
    for name, asset in document["assets"].items():
        asset["path"] = manifest["paths"].get("asset:" + name, manifest["original_assets"][name])
    document["restored_from"] = {
        "manifest_sha256": digest_file(source / "manifest.json"), "scope": manifest["scope"],
        "source_configuration_preserved": "original-deployment.json",
        "provenance_bytes_rewritten": False,
    }
    config_path = destination / "deployment.json"
    config_path.write_bytes(canonical(document))
    config = resolve(config_path, manifest["profile"])
    reference_checks = reference_asset_checks(config)
    _require_reference_checks(reference_checks)
    receipt = {"schema": "ashare-restore.v1", "deployment": str(config_path),
               "scope": manifest["scope"], "manifest_sha256": digest_file(source / "manifest.json"),
               "verified_files": len(manifest["files"]), "runtime_verified": True,
               "control_db": str(config.control_db), "paper_root": str(config.paper_root),
               "source_data_available": config.daily_root.is_dir(),
               "reference_assets": reference_checks,
               "provenance_bytes_rewritten": False}
    (destination / "restore-receipt.json").write_bytes(canonical(receipt))
    return receipt
