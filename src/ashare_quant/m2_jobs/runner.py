"""Trusted isolated wrapper around the installed source-pinned executor. Loaded by filename, not package import.

Only explicitly pinned package roots enter this interpreter. Checkpoint decoding is
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


def watch_control(control, execution_seconds):
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
            if time.monotonic() - started > execution_seconds:
                os._exit(75)
        except Exception:
            os._exit(74)
        time.sleep(0.1)


def execute(args):
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    if args["action"] == "run":
        assert args["control"] is not None
        threading.Thread(
            target=watch_control,
            args=(args["control"], args["resource_limits"]["execution_seconds"]),
            daemon=True,
        ).start()
    from rqprobe.guards import install_network_guard

    install_network_guard()
    from ashare_data import Store as DataStore

    from ashare_quant import __version__
    from ashare_quant.identity import source_inventory, source_sha256
    from ashare_quant.m2.common import digest, require
    from ashare_quant.m2.journal import Session
    from ashare_quant.m2.owner import OwnerClient, prepare
    from ashare_quant.m2.owner_wire import verify_runtime
    from ashare_quant.m2.parameter_policy import check_strategy, parse_parameters
    from ashare_quant.m2.portable import LocalDataBinding
    from ashare_quant.m2.registry import StrategyRegistry
    from ashare_quant.rq_mod import atomic_json
    from ashare_quant.runtime import runtime_fingerprint
    from rqprobe.runtime import runtime_identity

    assert __version__ == args["executor_version"]
    require(
        source_sha256() == args["executor_source_sha256"],
        "M2_JOB_EXECUTOR_CHANGED",
        "Executor source differs",
    )
    verify_runtime()
    root, owner = Path(args["root"]), DataStore(Path(args["store"]))
    session_file = root / "session.json"
    parameters = parse_parameters(args["parameters"])
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
    strategy_file = root / "strategy.json"
    require(
        not strategy_file.is_symlink()
        and strategy_file.is_file()
        and hashlib.sha256(strategy_file.read_bytes()).hexdigest() == args["strategy_sha256"],
        "M2_JOB_STRATEGY_CHANGED",
        "Frozen strategy bytes differ",
    )
    strategy = StrategyRegistry._decode(
        strategy_file.read_bytes(), json.loads(strategy_file.read_bytes())["version_id"]
    )
    check_strategy(parameters, strategy)
    from ashare_quant.python_strategies.spec import is_python, task_source
    task_source(root, strategy)
    run_id = "m2-native-" + args["job_id"]
    binding_file = root / "data-binding.json"
    require(
        not binding_file.is_symlink()
        and hashlib.sha256(binding_file.read_bytes()).hexdigest() == args["data_binding_sha256"],
        "M2_DATA_BINDING_CHANGED",
        "Frozen owner metadata differs",
    )
    binding = LocalDataBinding.model_validate_json(binding_file.read_bytes()).checked()
    entry = binding.entry(args["window"])
    from ashare_quant.m2.execution_dates import ExecutionScope, resolve
    scope_file = root / "execution-scope.json"
    require(not scope_file.is_symlink() and scope_file.is_file()
            and hashlib.sha256(scope_file.read_bytes()).hexdigest() == args["execution_scope_sha256"],
            "M2_EXECUTION_SCOPE_CHANGED", "Frozen execution prefix bytes differ")
    scope = ExecutionScope.model_validate_json(scope_file.read_bytes())
    require(scope == resolve(entry, scope.requested_end_date), "M2_EXECUTION_SCOPE_CHANGED",
            "Execution prefix must exactly copy admitted owner dates")
    if args["action"] == "prepare":
        require(
            not session_file.exists(),
            "M2_JOB_SESSION_EXISTS",
            "Preparation cannot replace a session",
        )
        client = prepare(
            owner,
            strategy,
            run_id=run_id,
            initial_cash=parameters.initial_cash,
            key=run_id,
            entry=entry,
            strategy_parameters=getattr(parameters, "strategy_parameters", {}),
            data_binding_sha256=binding.identity,
            end_date=scope.requested_end_date,
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
            and session.request.end_date == scope.requested_end_date
            and session.entry == entry
            and session.strategy == strategy
            and session.strategy_parameters == getattr(parameters, "strategy_parameters", {})
            and session.data_binding_sha256 == binding.identity
            and session.entry.owner_binding.owner_window_id == args["window"]
            and session.request.initial_cash == parameters.initial_cash,
            "M2_JOB_SESSION_CHANGED",
            "Frozen task scope differs",
        )
        client = OwnerClient.reopen(owner, session)
    session = client.session
    records = []
    for day in session.days:
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
    rq, runtime_pin = runtime_identity(), runtime_fingerprint(native=True)
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
                run_id=run_id,
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
        schema="m2-job-proof.v3",
        action=args["action"],
        executor_version=__version__,
        executor_source_sha256=source_sha256(),
        runner_sha256=args["runner_sha256"],
        parameters_sha256=digest(parameters),
        strategy_sha256=digest(strategy),
        python_source_sha256=strategy.source_sha256 if is_python(strategy) else None,
        data_binding_sha256=binding.identity,
        execution_scope_sha256=digest(scope),
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
                    "message": str(exc)[:1000],
                    "details": getattr(exc, "details", None),
                },
                ensure_ascii=False,
            )
        )
        raise
