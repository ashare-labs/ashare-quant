"""Supervise a single native process; mirror checkpoint-owned projections only."""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from ashare_quant.contracts import DomainError, EngineStep
from ashare_quant.storage.store import digest


def wire_receipt(value, key=""):
    monetary = {"cash", "frozen_cash", "equity", "average", "receivable", "transaction_cost",
                "price", "commission", "tax", "last", "bar", "fee"}
    if isinstance(value, dict):
        return {name: wire_receipt(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [wire_receipt(item, key) for item in value]
    if isinstance(value, str) and key in {"dt", "as_of", "end"}:
        clock = datetime.fromisoformat(value)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return clock.isoformat()
    if isinstance(value, float) or (key in monetary and isinstance(value, int) and not isinstance(value, bool)):
        with localcontext() as context:
            context.prec = 64
            return format(Decimal(str(value)).quantize(Decimal("0.00000001")), "f").rstrip("0").rstrip(".") or "0"
    return value



def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def run_native(store, run_id, token, manifest):
    root = store.path.parent / "engines" / run_id
    root.mkdir(parents=True, exist_ok=True)
    if store.run(run_id).cursor > 0 and not (root / "native.chk").is_file():
        raise DomainError("NATIVE_CHECKPOINT_MISSING", "Committed native projection exists but engine checkpoint is absent")
    account_lock = root.parent / "accounts" / (hashlib.sha256(manifest.request.account_id.encode()).hexdigest() + ".lock")
    account_lock.parent.mkdir(parents=True, exist_ok=True)
    publication = root / "publication.json"
    env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(root / "mpl")}
    env.pop("PROBE_KILL", None)
    env.pop("PROBE_OMIT_COMMISSION", None)
    with ExitStack() as locks:
        descriptors = []
        for path in (account_lock, root / "writer.lock"):
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            locks.callback(os.close, fd)  # Never LOCK_UN: a surviving child shares this description.
            while True:
                store.renew(run_id, token)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.05)
            descriptors.append(fd)
        # Recheck after waiting for an older writer. Neither cancellation nor stale leases bypass this.
        store.renew(run_id, token)
        current = store.run(run_id)
        reconcile = bool(current.cancel_requested_at or current.native_reconcile_only)

        def invoke(reconcile_only):
            invocation = str(uuid.uuid4())
            command = [sys.executable, "-m", "tests.support.synthetic.engine.launcher", "--scenario",
                manifest.strategy.definition.scenario, "--directory", str(root),
                "--account", manifest.request.account_id, "--account-lock", str(account_lock),
                "--invocation-id", invocation, "--run-id", run_id, "--manifest-sha256", digest(manifest),
                "--control-db", str(store.path), "--lease-token", token]
            for fd in descriptors:
                command += ["--inherited-lock", str(fd)]
            if reconcile_only:
                command.append("--reconcile-only")
            else:
                store.mark_native_started(run_id, token)
            last_publication = None
            data = None

            def project():
                nonlocal last_publication, data
                if not publication.exists():
                    return
                raw = publication.read_bytes()
                if raw == last_publication:
                    return
                candidate = json.loads(raw)
                if candidate.get("invocation_id") != invocation:
                    return
                # Only the current admitted child may export this checkpoint-owned projection.
                for item in candidate["receipt"]["engine_events"]:
                    store.commit_step(run_id, token, EngineStep.model_validate(item))
                last_publication, data = raw, candidate

            with (root / "process.log").open("ab") as log:
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                        pass_fds=tuple(descriptors))
                try:
                    while proc.poll() is None:
                        store.renew(run_id, token)
                        if not reconcile_only and store.run(run_id).cancel_requested_at:
                            _stop(proc)
                            break
                        project()
                        time.sleep(.05)
                    project()
                    return proc.returncode, data
                finally:
                    _stop(proc)

        code, data = invoke(reconcile)
        cancelled = store.run(run_id).cancel_requested_at is not None
        # A successful complete receipt is already frozen. Otherwise, a stopped writer must
        # be read through a separate no-execution admission path before cancellation can finish.
        if cancelled and not reconcile and not (code == 0 and data and data["complete"]):
            code, data = invoke(True)
        if code:
            raise DomainError("ENGINE_INTERRUPTED" if code < 0 else "NATIVE_ENGINE_REFUSED",
                              "Native writer stopped without an admitted result; checkpoint retained")
        if data is None:
            raise DomainError("NATIVE_ENGINE_REFUSED", "No admitted publication for this invocation")
        if not data["checkpoint_present"] and store.run(run_id).cursor:
            raise DomainError("NATIVE_CHECKPOINT_MISSING", "Cannot replace committed projection with an empty result")
        if not data["complete"] and not cancelled:
            raise DomainError("NATIVE_OPERATOR_REVIEW_REQUIRED" if reconcile else "NATIVE_ENGINE_REFUSED",
                              "Native checkpoint is partial; no terminal result was fabricated")
        receipt = wire_receipt(data["receipt"])
        receipt["host_reconciliation"] = {"checkpoint_present": data["checkpoint_present"],
                                           "native_complete": data["complete"]}
        # Locks remain held until result, terminal status and account release commit together.
        store.complete_native(run_id, token, receipt, native_complete=data["complete"])
