"""Trusted isolated wrapper around the sealed parameter execution revision. Loaded by filename, not package import.

Only the frozen executor's modules enter this interpreter. Checkpoint decoding is
confined here, with local writer exclusion and frozen session/header checks.
This process boundary is not an adversarial Python-code sandbox.
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def watch_control(control):
    """Stop only this process when ownership/cancellation changes, including orphaned workers."""
    started = time.monotonic()
    while True:
        try:
            uri = Path(control["database"]).as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as db:
                row = db.execute(
                    "SELECT status,lease_token,lease_until,cancellation_requested_at FROM m2_jobs WHERE job_id=?",
                    (control["job_id"],),
                ).fetchone()
            if (
                not row
                or row[0] not in {"running", "cancelling"}
                or row[1] != control["token"]
                or row[2] <= time.time()
            ):
                os._exit(74)
            if row[3]:
                os._exit(73)
            if time.monotonic() - started > 120:
                os._exit(75)
        except Exception:
            os._exit(74)
        time.sleep(0.1)


def execute(args):
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    if args["action"] == "run":
        assert args["control"] is not None
        threading.Thread(target=watch_control, args=(args["control"],), daemon=True).start()
    from rqprobe.guards import install_network_guard

    install_network_guard()
    from ashare_data import Store as DataStore
    from ashare_data.m2_types import canonical_hash

    from ashare_quant import __version__
    from ashare_quant.identity import source_inventory, source_sha256
    from ashare_quant.m2.common import digest, require
    from ashare_quant.m2.journal import Session
    from ashare_quant.m2.owner import OwnerClient, prepare
    from ashare_quant.m2.owner_wire import release, review, verify_runtime
    from ashare_quant.m2.parameter_policy import BandParameters
    from ashare_quant.m2.registry import StrategyRegistry
    from ashare_quant.rq_mod import atomic_json
    from ashare_quant.runtime import runtime_fingerprint
    from ashare_quant.store import Store
    from rqprobe.runtime import runtime_identity

    assert __version__ == args["executor_version"] == "0.4.0.dev15+exec1"
    require(
        source_sha256() == args["executor_source_sha256"],
        "M2_JOB_EXECUTOR_CHANGED",
        "Executor source differs",
    )
    verify_runtime()
    review()
    root, owner = Path(args["root"]), DataStore(Path(args["store"]))
    session_file = root / "session.json"
    parameters = BandParameters.model_validate(args["parameters"])
    parameter_file = root / "parameters.json"
    require(
        not parameter_file.is_symlink()
        and hashlib.sha256(parameter_file.read_bytes()).hexdigest()
        == args["parameters_sha256"]
        == digest(parameters)
        and json.loads(parameter_file.read_bytes()) == parameters.model_dump(mode="json"),
        "M2_JOB_PARAMETERS_CHANGED",
        "Frozen parameters differ",
    )
    definition = parameters.definition()
    run_id = "m2-native-" + args["window"] + "-" + digest(str(root))[:12]
    if args["action"] == "prepare":
        require(
            not session_file.exists(),
            "M2_JOB_SESSION_EXISTS",
            "Preparation cannot replace a session",
        )
        pin = release()
        window = next(w for w in pin["windows"] if w["window_id"] == args["window"])
        profile = next(
            p for p in pin["owner_pins"] if canonical_hash(p) == window["owner_pin_sha256"]
        )["profile_sha256"]
        strategy = StrategyRegistry(Store(root / "control.sqlite")).register(definition)
        client = prepare(
            owner,
            strategy,
            run_id=run_id,
            initial_cash=parameters.initial_cash,
            key=run_id,
            profile_sha256=profile,
            window_id=args["window"],
        )
        atomic_json(session_file, client.session.model_dump(mode="json"))
    else:
        require(
            not session_file.is_symlink()
            and hashlib.sha256(session_file.read_bytes()).hexdigest()
            == args["session_file_sha256"],
            "M2_JOB_SESSION_CHANGED",
            "Frozen session bytes differ",
        )
        session = Session.model_validate_json(session_file.read_text())
        require(
            session.run_id == run_id
            and session.strategy.definition == definition
            and session.entry.owner_binding.owner_window_id == args["window"]
            and session.request.initial_cash == parameters.initial_cash,
            "M2_JOB_SESSION_CHANGED",
            "Frozen task scope differs",
        )
        client = OwnerClient.reopen(owner, session)
    session = client.session
    records = []
    for day in session.entry.windows[0].days:
        for event, at in [
            ("BEFORE_TRADING", "09:00:00"),
            ("BAR", "15:00:00"),
            ("AFTER_TRADING", "15:01:00"),
        ]:
            records.append(
                dict(
                    cursor=len(records) + 1, event=event, dt=day.trading_date.isoformat() + "T" + at
                )
            )
    rq, runtime_pin = runtime_identity(), runtime_fingerprint()
    identity = digest(
        dict(
            schema="m2.native.experiment.v1",
            session=session.identity,
            rq=rq,
            runtime=runtime_pin,
            backend_sources=source_inventory(),
            records=records,
        )
    )
    if args["action"] == "run":
        from ashare_quant.m2_native.experiment import run

        run(
            SimpleNamespace(
                root=root,
                store=Path(args["store"]),
                window=args["window"],
                invocation=args["invocation"],
                fault=args["fault"],
                parameters=parameters.model_dump(mode="json"),
            )
        )
    elif args["action"] not in {"prepare", "inspect"}:
        raise RuntimeError("M2_JOB_UNKNOWN_ACTION")

    # No ledger reconstruction: validate the same atomic native payload and
    # project its committed receipt. Attempt traces never become result inputs.
    from ashare_quant.m2_native.mod import CheckedCheckpoint
    from ashare_quant.m2_native.state import runtime

    runtime["client"] = client
    checkpoint = root / "native.chk"
    require(
        not checkpoint.is_symlink(), "M2_JOB_CHECKPOINT_LINK", "Checkpoint must belong to this task"
    )
    payload = CheckedCheckpoint(checkpoint, identity).read()
    cursor, complete, pending, receipt_sha = 0, False, False, None
    if payload:
        state = json.loads(payload["states"]["mod_m2native"])
        cursor = json.loads(payload["states"]["event_source"])["cursor"]
        journal = state["journal"]["m2_state"]
        complete = payload["complete"]
        pending = len(journal["decisions"]) > len(journal["executions"])
        if complete:
            receipt = payload["receipt"]
            require(
                receipt["journal"] == state["journal"]
                and receipt["identity"] == identity
                and receipt["session_identity"] == session.identity
                and journal["complete"]
                and cursor == len(records),
                "M2_JOB_ATOMIC_RECEIPT",
                "Completed native receipt is not atomic",
            )
            for a, b in [
                ("reads", "owner_reads"),
                ("observations", "native_observations"),
                ("trades", "native_trades"),
                ("orders", "native_orders"),
                ("accounts", "native_accounts"),
            ]:
                require(
                    state[a] == receipt[b],
                    "M2_JOB_ATOMIC_RECEIPT",
                    "Receipt differs from committed native state",
                )
            receipt_path = root / (args["invocation"] + ".receipt.json")
            atomic_json(receipt_path, receipt)
            receipt_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    completion_path = root / (args["invocation"] + ".completion.json")
    completion = json.loads(completion_path.read_text()) if completion_path.exists() else None
    proof = dict(
        schema="m2-job-proof.v1",
        action=args["action"],
        executor_version=__version__,
        executor_source_sha256=source_sha256(),
        runner_sha256=args["runner_sha256"],
        parameters_sha256=digest(parameters),
        session_sha256=session.identity,
        session_file_sha256=hashlib.sha256(session_file.read_bytes()).hexdigest(),
        native_identity=identity,
        records=records,
        runtime=runtime_pin,
        rq=rq,
        cursor=cursor,
        complete=complete,
        pending_submit=pending,
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        if checkpoint.exists()
        else None,
        receipt_file_sha256=receipt_sha,
        completion=completion,
        historical_pit_verified=False,
        paper_admitted=False,
    )
    atomic_json(root / (args["invocation"] + ".proof.json"), proof)


if __name__ == "__main__":
    args = globals()["TASK_ARGUMENTS"]
    try:
        execute(args)
    except Exception as exc:
        path = Path(args["root"]) / (args["invocation"] + ".runner-error.json")
        path.write_text(
            json.dumps(
                {
                    "type": type(exc).__name__,
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        raise
