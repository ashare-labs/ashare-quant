"""Run in a separate trusted process; process isolation is not a hostile-code sandbox."""

import sqlite3

from ashare_quant.admission import validate_manifest
from ashare_quant.backtests.synthetic import FixtureReplayAdapter
from ashare_quant.contracts import DomainError
from ashare_quant.identity import source_sha256
from ashare_quant.storage.store import Store


def retryable_storage(exc):
    """Known I/O failures may be retried; schema/corruption/programming faults need review.

    SQL trigger aborts are the explicit removable transport-fault seam. Ordinary
    constraint failures are not silently treated as transient. Exit the worker on
    every failure; this classification never adds an automatic retry loop.
    """
    if isinstance(exc, OSError):
        return True
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        return isinstance(exc, sqlite3.OperationalError)
    return code == sqlite3.SQLITE_CONSTRAINT_TRIGGER or code & 0xff in {
        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_PROTOCOL, sqlite3.SQLITE_AUTH,
        sqlite3.SQLITE_PERM,
    }


def work_one(store: Store, run_id: str | None = None) -> str | None:
    claim = store.claim(run_id)
    if claim is None:
        return None
    run_id, token, checkpoint = claim
    native = None  # Unknown until the accepted manifest is successfully read and parsed.
    phase = "manifest"
    try:
        manifest = store.manifest(run_id)
        native = manifest.engine.engine_id == "rqalpha"
        phase = "admission"
        if manifest.backend_source_sha256 != source_sha256():
            raise DomainError("BACKEND_CHANGED", "Installed implementation differs from accepted run")
        validate_manifest(manifest)
        scenario = manifest.strategy.definition.scenario
        if native:
            from ashare_quant.execution.synthetic.process import run_native
            phase = "native"
            run_native(store, run_id, token, manifest)
            return run_id
        phase = "fixture_setup"
        engine = FixtureReplayAdapter(scenario, manifest.fixture_sha256)
        if engine.identity != manifest.engine:
            raise DomainError("ENGINE_CHANGED", "Account engine identity cannot change on resume")
        while True:
            phase = "fixture_execution"
            step = engine.next_step(checkpoint)
            if step is None:
                break
            phase = "projection"
            store.commit_step(run_id, token, step)
            checkpoint = step.checkpoint
        phase = "finalization"
        store.finish(run_id, token)
    except Exception as exc:
        # Cancellation or another worker's lease ownership must never be overwritten.
        if isinstance(exc, DomainError) and exc.code == "STALE_WORKER":
            raise
        if isinstance(exc, (sqlite3.Error, OSError)):
            # Covers the entire post-claim interval, including unknown engine identity.
            # If this read or the release transaction also fails, leave the lease/account
            # untouched. Its expiry allows recovery; never attempt terminal fallback.
            current = store.run(run_id)
            if current.status in {"succeeded", "cancelled"} and not current.reconciliation_required:
                return run_id  # Acknowledgement lost after a committed terminal transaction.
            native_root = store.path.parent / "engines" / run_id
            proven_prestart_obstruction = (native is True and phase == "native"
                and isinstance(exc, FileExistsError) and not current.native_started
                and not current.cancel_requested_at and current.cursor == 0 and native_root.is_file())
            if proven_prestart_obstruction:
                store.finish(run_id, token, "Worker failed: FileExistsError", prestart_obstruction=True)
                raise
            retry = retryable_storage(exc)
            reason = ("NATIVE_RECONCILIATION_REQUIRED" if native or current.reconciliation_required
                      else "CONTROL_STORAGE_RETRY_REQUIRED") if retry else "CONTROL_STORAGE_REVIEW_REQUIRED"
            store.release(run_id, token, f"{reason}: {phase}: {type(exc).__name__}", reconcile_only=not retry)
            raise
        if isinstance(exc, DomainError) and exc.code in {
            "BACKEND_CHANGED", "DEPENDENCIES_CHANGED", "RUNTIME_FINGERPRINT_MISSING",
            "RUNTIME_DEPENDENCY_MISMATCH", "SPEC_CHANGED", "ENGINE_CHANGED",
            "FIXTURE_CHANGED", "CHECKPOINT_MISMATCH", "CHECKPOINT_INVALID",
            "ENGINE_INTERRUPTED", "NATIVE_ENGINE_REFUSED",
            "NATIVE_CHECKPOINT_MISSING",
            "NATIVE_OPERATOR_REVIEW_REQUIRED",
        }:
            store.release(run_id, token, f"Admission blocked: {exc.code}",
                          reconcile_only=exc.code in {"NATIVE_OPERATOR_REVIEW_REQUIRED", "NATIVE_ENGINE_REFUSED"})
            raise
        if native is not False or phase not in {"fixture_setup", "fixture_execution"}:
            reason = "NATIVE_HOST_REVIEW_REQUIRED" if native else "CONTROL_OPERATOR_REVIEW_REQUIRED"
            store.release(run_id, token, f"{reason}: {phase}: {type(exc).__name__}", reconcile_only=True)
            raise
        code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
        # Avoid logging arbitrary exception payloads that could contain local secrets.
        store.finish(run_id, token, f"Worker failed: {code}")
        raise
    return run_id
