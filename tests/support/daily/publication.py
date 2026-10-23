"""Atomic, sealed publication and fail-closed reads of dynamic native results."""

import json
import os
import shutil
import uuid

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import (
    atomic,
    canonical,
    digest,
    regular,
    require,
    sha,
    sync_dir,
)
from ashare_quant.tasks.release import EXECUTION_VERSION
from tests.support.daily.contracts import JobResult, ResultReference
from tests.support.daily.projection import project
from tests.support.legacy_python.spec import is_python

NAMES = {
    "data-binding.json",
    "execution-scope.json",
    "parameters.json",
    "strategy.json",
    "result.json",
    "manifest.json",
    "session.json",
    "receipt.json",
    "proof.json",
    "native.chk",
    "history.json",
    "native-manifests.json",
}


def names(manifest):
    return NAMES | ({"strategy-source.py"} if is_python(manifest.strategy) else set())


def check_proof(manifest, proof, checkpoint, session):
    require(
        proof["schema"] == "m2-job-proof.v3"
        and proof["executor_version"] == EXECUTION_VERSION
        and proof["data_binding_sha256"] == manifest.data_binding_sha256
        and proof["execution_scope_sha256"] == digest(manifest.execution_scope)
        and proof["parameters_sha256"] == manifest.parameters_sha256
        and proof["strategy_sha256"] == digest(manifest.strategy)
        and proof["python_source_sha256"] == manifest.python_source_sha256
        and proof["executor_source_sha256"] == manifest.executor_source_sha256
        and proof["runner_sha256"] == manifest.runner_sha256
        and proof["session_sha256"] == manifest.session_sha256
        and proof["session_file_sha256"] == manifest.session_file_sha256 == sha(session)
        and json.loads(session) == manifest.session
    )
    for key in ("native_identity", "records", "runtime", "rq"):
        require(proof[key] == manifest.prepared_proof[key])
    require(
        proof["complete"]
        and proof["cursor"] == len(proof["records"])
        and proof["checkpoint_sha256"] == sha(checkpoint)
    )
    header, payload = checkpoint.split(b"\n", 1)
    require(
        json.loads(header)
        == {"schema": 1, "identity": proof["native_identity"], "sha256": sha(payload)}
    )


def _project(manifest, captured):
    require(captured["execution-scope.json"] == canonical(manifest.execution_scope))
    if is_python(manifest.strategy):
        require(sha(captured["strategy-source.py"]) == manifest.python_source_sha256 == manifest.strategy.source_sha256)
    require(sha(captured["data-binding.json"]) == manifest.data_binding_sha256)
    require(captured["manifest.json"] == canonical(manifest))
    require(captured["parameters.json"] == canonical(manifest.request.parameters))
    require(
        captured["strategy.json"] == canonical(manifest.strategy)
        and manifest.session["strategy"] == manifest.strategy.model_dump(mode="json")
    )
    proof = json.loads(captured["proof.json"])
    check_proof(manifest, proof, captured["native.chk"], captured["session.json"])
    require(sha(captured["receipt.json"]) == proof["receipt_file_sha256"])
    history = json.loads(captured["history.json"])
    manifests = json.loads(captured["native-manifests.json"])
    require(bool(manifests), message="No actual native invocation manifest exists")
    for entry in manifests:
        require(
            entry["identity"] == proof["native_identity"]
            and entry["session"] == manifest.session
            and entry["records"] == proof["records"]
            and entry["runtime"] == proof["runtime"]
            and entry["rq"] == proof["rq"]
        )
        # Native source identity intentionally uses the original inventory JSON format.
        require(
            sha(json.dumps(entry["source_inventory"], sort_keys=True).encode())
            == manifest.executor_source_sha256
        )
    return project(
        manifest,
        json.loads(captured["receipt.json"]),
        proof,
        {
            name: {"sha256": sha(raw), "size_bytes": len(raw)}
            for name, raw in captured.items()
            if name != "result.json"
        },
        {
            "control_events": history,
            "final_cursor": proof["cursor"],
            "checkpoint_complete": True,
            "completion": proof["completion"],
            "native_generation": json.loads(captured["receipt.json"])["generation"],
            "checkpoint_proof_action": proof["action"],
        },
    )


def publish(job_dir, manifest, invocation, attempt, history):
    captured = {
        "data-binding.json": regular(job_dir / "data-binding.json"),
        "execution-scope.json": regular(job_dir / "execution-scope.json"),
        "parameters.json": regular(job_dir / "parameters.json"),
        "strategy.json": regular(job_dir / "strategy.json"),
        "manifest.json": canonical(manifest),
        "session.json": regular(job_dir / "session.json"),
        "receipt.json": regular(job_dir / (invocation + ".receipt.json")),
        "proof.json": regular(job_dir / (invocation + ".proof.json")),
        "native.chk": regular(job_dir / "native.chk"),
        "history.json": canonical(history),
        "native-manifests.json": canonical(
            [json.loads(regular(p)) for p in sorted(job_dir.glob("attempt-*.manifest.json"))]
        ),
    }
    if is_python(manifest.strategy):
        captured["strategy-source.py"] = regular(job_dir / "strategy-source.py")
    result = _project(manifest, captured)
    captured["result.json"] = canonical(result)
    seal = {
        "schema": "m2-job-publication.v3",
        "job_id": manifest.job_id,
        "manifest_sha256": digest(manifest),
        "files": {name: {"sha256": sha(raw), "size": len(raw)} for name, raw in captured.items()},
    }
    name = "publication-" + str(attempt)
    target = job_dir / name
    require(not target.exists(), "M2_JOB_PUBLICATION_EXISTS", "Publication cannot be overwritten")
    temp = job_dir / ("publish-" + uuid.uuid4().hex + ".pending")
    temp.mkdir(mode=0o700)
    try:
        for filename, raw in captured.items():
            atomic(temp / filename, raw)
        atomic(temp / "seal.json", seal)
        os.replace(temp, target)
        sync_dir(job_dir)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return ResultReference(
        result_id=manifest.job_id,
        href="/api/v1/tasks/daily/jobs/" + manifest.job_id + "/result",
        snapshot_sha256=digest(result),
        evidence_manifest_sha256=digest(seal),
        publication=name,
    )


def read(job_dir, manifest, reference):
    try:
        root = job_dir / reference.publication
        require(not root.is_symlink())
        require({p.name for p in root.iterdir()} == names(manifest) | {"seal.json"})
        seal_raw = regular(root / "seal.json")
        require(sha(seal_raw) == reference.evidence_manifest_sha256)
        seal = json.loads(seal_raw)
        require(
            seal["schema"] == "m2-job-publication.v3"
            and seal["job_id"] == manifest.job_id
            and seal["manifest_sha256"] == digest(manifest)
            and set(seal["files"]) == names(manifest)
        )
        captured = {}
        for name, pin in seal["files"].items():
            raw = regular(root / name)
            require(sha(raw) == pin["sha256"] and len(raw) == pin["size"])
            captured[name] = raw
        result = _project(manifest, captured)
        require(
            canonical(result) == captured["result.json"]
            and digest(result) == reference.snapshot_sha256
        )
        require(JobResult.model_validate_json(captured["result.json"]) == result)
        # Detect removal/replacement during projection, even after prior success.
        require({p.name for p in root.iterdir()} == names(manifest) | {"seal.json"} and not root.is_symlink())
        require(regular(root / "seal.json") == seal_raw)
        for name, raw in captured.items():
            require(regular(root / name) == raw)
        return result
    except (OSError, ValueError, KeyError, TypeError, DomainError) as exc:
        raise DomainError(
            "M2_JOB_RESULT_CONFLICT",
            "Dynamic result evidence is missing, changed or inconsistent; discard prior success",
            409,
        ) from exc
