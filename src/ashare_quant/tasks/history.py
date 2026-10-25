"""Read frozen task evidence without importing or executing its former algorithms.

Hashes establish consistency with the stored acceptance/publication, not a new
economic audit or permission to execute a previous runtime.
"""

import errno
import json
from contextlib import contextmanager

from ashare_quant.contracts import DomainError
from ashare_quant.storage.artifacts import canonical, digest, require, sha
from ashare_quant.tasks.base import TaskService
from ashare_quant.tasks.contracts import execution_presentation
from ashare_quant.tasks.evidence import (
    CHUNK_BYTES,
    evidence_file,
    evidence_hash,
    project_json,
    verified_copy,
)
from ashare_quant.tasks.progress import snapshot

FAMILIES = {
    "m2-job-manifest.v6": "daily",
    "portfolio-job-manifest.v1": "portfolio",
    "python-minute-job.v1": "python",
    "python-demand-job.v2": "python",
    "python-project-job.v3": "python",
    "factor-job.v1": "factors",
}


class History(TaskService):
    def check(self, row, *, execution=False):
        if execution:
            raise DomainError("HISTORY_READ_ONLY", "History cannot execute strategies", 409)
        try:
            manifest = json.loads(row["manifest"])
            request = json.loads(row["request"])
            require(
                manifest["schema_version"] in FAMILIES
                and manifest["job_id"] == row["job_id"]
                and digest(manifest) == row["manifest_sha256"]
                and manifest["request"] == request
                and digest(request) == row["request_hash"],
                "HISTORY_IDENTITY_CONFLICT", "Frozen task identity differs",
            )
            root = self.directory(row["job_id"])
            require(evidence_file(root, "accepted-manifest.json") == canonical(manifest),
                    "HISTORY_IDENTITY_CONFLICT", "Accepted manifest differs")
            require(digest(manifest["session"]) == manifest["session_sha256"]
                    and sha(evidence_file(root, "session.json")) == manifest["session_file_sha256"]
                    and json.loads(evidence_file(root, "session.json")) == manifest["session"],
                    "HISTORY_IDENTITY_CONFLICT", "Frozen session differs")
            for name, expected in manifest.get("files", {}).items():
                require(evidence_hash(root, name)[0] == expected,
                        "HISTORY_EVIDENCE_CONFLICT", "Frozen input differs")
            if manifest["schema_version"] == "python-project-job.v3":
                from ashare_quant.tasks.project_evidence import verify_project

                verify_project(root, manifest)
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError("HISTORY_EVIDENCE_CONFLICT", "Frozen task is unavailable or malformed", 409) from exc

    def view(self, row):
        manifest = self.check(row)
        presentation = execution_presentation(row["status"], manifest, self.source)
        result = json.loads(row["result"]) if row["result"] else None
        if result:
            result = {**result, "href": f"/api/v1/history/jobs/{row['job_id']}/result"}
        return {
            "job_id": row["job_id"], "family": FAMILIES[manifest["schema_version"]],
            "request": manifest["request"], "status": presentation["display_status"],
            **presentation,
            "account_id": row["account_id"], "manifest_sha256": row["manifest_sha256"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "attempt_count": row["attempt_count"], "result": result,
            "error": json.loads(row["error"]) if row["error"] else None,
            "read_only": True, "can_resume": False,
            "progress": snapshot(self.directory(row["job_id"]), row),
            "original_service_identity": manifest.get("service_identity"),
            "verification": "frozen_integrity_only",
        }

    def list(self, family=None):
        return {"items": [self.view(r) for r in self.repo.rows()
                          if family is None or FAMILIES.get(json.loads(r["manifest"]).get("schema_version")) == family]}

    def result(self, job_id):
        with self.result_stream(job_id) as (stream, _, _):
            return json.load(stream)

    def result_projection(self, job_id):
        with self.result_stream(job_id) as (stream, _, _):
            return project_json(stream, {"job_id", "summary", "preparation", "native"})

    def result_response(self, job_id):
        from starlette.responses import StreamingResponse

        context = self.result_stream(job_id)
        stream, _, _ = context.__enter__()  # Fail integrity checks before HTTP 200.

        def chunks():
            try:
                while block := stream.read(CHUNK_BYTES):
                    yield block
            finally:
                context.__exit__(None, None, None)

        from starlette.background import BackgroundTask

        return StreamingResponse(chunks(), media_type="application/json",
                                 background=BackgroundTask(context.__exit__, None, None, None))

    def analysis(self, job_id):
        from ashare_quant.tasks.result_analysis import analyze

        with self.result_stream(job_id) as (stream, snapshot_sha256, family):
            if family != "python":
                raise DomainError("RESULT_ANALYSIS_UNSUPPORTED", "Analysis requires a frozen Python account result", 422)
            result = project_json(stream, {"job_id", "summary", "preparation", "native"})
        return analyze(result, snapshot_sha256)

    @contextmanager
    def result_stream(self, job_id):
        """Verify the entire publication, then expose a private temporary stream."""
        row = self.repo.row(job_id)
        manifest = self.check(row)
        require(row["status"] == "succeeded" and row["result"],
                "M2_JOB_RESULT_NOT_READY", "Task has no published result")
        try:
            reference = json.loads(row["result"])
            require(reference["result_id"] == job_id, "HISTORY_IDENTITY_CONFLICT")
            root = self.directory(job_id)
            if manifest["schema_version"] in {"python-minute-job.v1", "python-demand-job.v2", "python-project-job.v3", "factor-job.v1"}:
                require(reference["evidence_manifest_sha256"] == digest(manifest),
                        "HISTORY_IDENTITY_CONFLICT")
                result_root, result_name = root, "published.json"
            else:
                import re

                name = reference["publication"]
                require(re.fullmatch(r"publication-[1-9][0-9]*", name) is not None,
                        "HISTORY_EVIDENCE_PATH")
                sealed = root / name
                seal_raw = evidence_file(sealed, "seal.json")
                require(sha(seal_raw) == reference["evidence_manifest_sha256"],
                        "HISTORY_EVIDENCE_CONFLICT", "Publication seal differs")
                seal = json.loads(seal_raw)
                require(seal["job_id"] == job_id and seal["manifest_sha256"] == digest(manifest)
                        and {"manifest.json", "session.json", "result.json", "receipt.json", "proof.json", "native.chk"} <= seal["files"].keys(),
                        "HISTORY_EVIDENCE_CONFLICT")
                require(seal["files"]["result.json"]["sha256"] == reference["snapshot_sha256"]
                        and seal["files"]["manifest.json"]["sha256"] == sha(canonical(manifest)),
                        "HISTORY_EVIDENCE_CONFLICT", "Publication pins differ from the accepted task/result")
                for filename, pin in seal["files"].items():
                    hashed, size = evidence_hash(sealed, filename)
                    require(hashed == pin["sha256"] and size == pin["size"],
                            "HISTORY_EVIDENCE_CONFLICT", "Sealed evidence differs")
                result_root, result_name = sealed, "result.json"
            with verified_copy(result_root, result_name, reference["snapshot_sha256"], self.repo.policy) as stream:
                require(project_json(stream, {"job_id"})["job_id"] == job_id, "HISTORY_IDENTITY_CONFLICT")
                yield stream, reference["snapshot_sha256"], FAMILIES[manifest["schema_version"]]
        except MemoryError as exc:
            raise DomainError("HISTORY_MEMORY_UNAVAILABLE", "Insufficient memory to read selected result fields", 503) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise DomainError("STORAGE_FULL", "Insufficient space for a verified result read", 507) from exc
            if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
                raise DomainError("HISTORY_MEMORY_UNAVAILABLE", "Insufficient memory to read selected result fields", 503) from exc
            raise DomainError("HISTORY_RESULT_CONFLICT", "Published result is unavailable or malformed", 409) from exc

    def strategies(self):
        with self.repo.store.connection() as db:
            rows = db.execute("SELECT r.version_id,r.body,p.source FROM registered_strategies r "
                              "LEFT JOIN python_sources p ON p.version_id=r.version_id "
                              "ORDER BY r.version_id").fetchall()
        items = []
        for version_id, body, source in rows:
            frozen = json.loads(body)
            item = dict(version_id=version_id, frozen=frozen, read_only=True)
            if source is not None:
                require(len(source) <= 65536 and sha(source) == frozen.get("source_sha256"),
                        "HISTORY_SOURCE_CONFLICT", "Registered Python source differs")
                item["source"] = source.decode("utf-8")
            items.append(item)
        return {"items": items}
